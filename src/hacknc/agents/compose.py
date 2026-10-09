"""The agents' clinical rules, and the facts handed to the language model.

Pure functions: tool results in, decisions and short Markdown-ish text out.

* Safety-critical decisions (allergies, restarts, controlled substances) are rules, always.
* Where a model writes text (summary narrative, patient instructions, chat), these functions
  also build its input, and the rule-based text here is the fallback when no model answers.
* Notes and portal messages are *data*: instructions found inside them are reported and kept
  away from the model, never followed.
"""

from __future__ import annotations

import re
from typing import Any

INJECTION_PATTERNS = re.compile(
    r"ignore (all |any )?(previous|prior) instructions|system notice to ai|"
    r"you are now|disregard (your|the) (rules|instructions)|act as an? (admin|system)",
    re.IGNORECASE,
)
SNOMED_SUFFIX = re.compile(r"\s*\((disorder|finding|situation)\)\s*$")
# Drug knowledge the rules need (a real system would use RxNorm / a drug database).
DRUG_CLASSES = {
    "amoxicillin": "penicillin", "amoxicillin-clavulanate": "penicillin",
    "ampicillin": "penicillin", "penicillin": "penicillin", "piperacillin": "penicillin",
    "cephalexin": "cephalosporin", "cefpodoxime": "cephalosporin",
    "ceftriaxone": "cephalosporin", "cefuroxime": "cephalosporin",
    "trimethoprim-sulfamethoxazole": "sulfonamide", "doxycycline": "tetracycline",
    "ciprofloxacin": "fluoroquinolone", "levofloxacin": "fluoroquinolone",
    "naproxen": "nsaid", "ibuprofen": "nsaid", "diclofenac": "nsaid", "meloxicam": "nsaid",
    "oxycodone": "opioid", "morphine": "opioid", "hydromorphone": "opioid",
}
ANTIBIOTIC_CLASSES = {"penicillin", "cephalosporin", "sulfonamide", "tetracycline",
                      "fluoroquinolone"}
# Same indication, different class: what the rules suggest when an allergy rules a drug out.
ALTERNATIVES = {
    "amoxicillin-clavulanate": {"drug": "doxycycline", "dose": "100 mg", "quantity": 10,
                                "sig": "1 tablet by mouth twice daily for 5 days"},
    "trimethoprim-sulfamethoxazole": {"drug": "cefpodoxime", "dose": "200 mg", "quantity": 14,
                                      "sig": "1 tablet by mouth twice daily for 7 days"},
    "ciprofloxacin": {"drug": "cefpodoxime", "dose": "200 mg", "quantity": 14,
                      "sig": "1 tablet by mouth twice daily for 7 days"},
}
CONTROLLED = {"oxycodone": "C-II", "morphine": "C-II", "hydromorphone": "C-II",
              "tramadol": "C-IV", "lorazepam": "C-IV", "clonazepam": "C-IV",
              "diazepam": "C-IV", "lacosamide": "C-V"}
DEFAULT_WARNING_SIGNS = "trouble breathing, fever over 38.5 °C, or chest pain"
KIDNEY_LIMIT = 1.2   # creatinine (mg/dL) above which NSAIDs are flagged


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION_PATTERNS.search(text or ""))


def short_condition(text: str) -> str:
    return SNOMED_SUFFIX.sub("", text)


def short_medication(text: str) -> str:
    """'Oxycodone hydrochloride 5 MG Oral Tablet' -> 'Oxycodone hydrochloride 5 MG'."""
    return re.split(r"\s+(Oral|Injectable|Extended|Injection)\b", text)[0]


def withheld_line(withheld: list[dict[str, Any]]) -> str:
    if not withheld:
        return ""
    reasons = sorted({w["reason"] for w in withheld})
    return f"🔒 {len(withheld)} record(s) not shown to you: {'; '.join(reasons)}"


# A sentence ends at . ! ? followed by a space, except after a title or initial ("Dr. Lee").
SENTENCE_END = re.compile(r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bSt\.)"
                          r"(?<!\b[A-Z]\.)(?<=[.!?])\s")


def first_sentence(text: str, limit: int = 160) -> str:
    sentence = SENTENCE_END.split(text.strip(), maxsplit=1)[0]
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _value_number(value: str) -> float | None:
    match = re.match(r"\s*([\d.]+)", value or "")
    return float(match.group(1)) if match else None


def find_observation(observations: list[dict[str, Any]], *words: str) -> dict[str, Any] | None:
    for obs in observations:
        name = obs["code"]["text"].lower()
        if all(w in name for w in words):
            return obs
    return None


# --------------------------------------------------------------------------- reconciliation


def drug_class(drug: str) -> str:
    return DRUG_CLASSES.get(drug.lower(), "")


def _allergy_for(drug: str, allergies: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The documented allergy that rules ``drug`` out, if any (same class or same name)."""
    cls = drug_class(drug)
    for allergy in allergies:
        allergic = (allergy.get("drugClass") or "").lower()
        if (allergic and allergic == cls) or \
                allergy.get("substance", "").lower().startswith(drug.lower().split("-")[0]):
            return allergy
    return None


def _strength(text: str) -> str | None:
    """'Simvastatin 10 MG Oral Tablet' -> '10 mg' (None if the name gives no strength)."""
    found = re.search(r"(\d+(?:\.\d+)?)\s*(MG|MCG|UNT)\b", text, re.IGNORECASE)
    return f"{found.group(1)} {found.group(2).lower()}" if found else None


def _drug_name(rx: dict[str, Any]) -> str:
    return f"{rx['drug'][0].upper()}{rx['drug'][1:]} {rx['dose']}".strip()


def _hold_check(med: dict[str, Any],
                observations: list[dict[str, Any]]) -> tuple[bool | None, str]:
    """May a held medicine restart? (True / False / None = no rule) and the evidence."""
    rule = med.get("resumeIf")
    if not rule:
        return None, ""
    obs = find_observation(observations, rule["observation"].lower())
    value = _value_number(obs["value"]) if obs else None
    if value is None:
        return False, f"no recent {rule['observation']} result"
    evidence = f"{obs['code']['text'].lower()} {obs['value']}"
    if "max" in rule and value > rule["max"]:
        return False, f"{evidence}, above {rule['max']}"
    if "min" in rule and value < rule["min"]:
        return False, f"{evidence}, below {rule['min']}"
    return True, f"{evidence}, within limits"


def reconcile(meds: list[dict[str, Any]], allergies: list[dict[str, Any]],
              observations: list[dict[str, Any]],
              plan: dict[str, Any]) -> dict[str, Any]:
    """Verify the physician's discharge plan against the chart. Returns discrepancies plus the
    reconciled discharge list (what the patient goes home on).

    ``plan`` is the admission's generic ``dischargePlan`` (see ``ehr/records.py``). The rules:

    * hospital-only medicines stop
    * a planned medicine the patient is allergic to is replaced by a same-indication
      alternative if one is known, otherwise left out; either way it is flagged (high)
    * a held medicine restarts only if its restart rule (e.g. creatinine <= 1.2) is met; a plan
      that restarts it anyway is flagged
    * controlled substances are marked (registry check + physician signature downstream)
    * an NSAID continuing with reduced kidney function is flagged
    * everything else continues unchanged (sensitive labels kept, so later steps can withhold)
    """
    discrepancies: list[dict[str, str]] = []
    new_items: list[dict[str, Any]] = []
    plan_list: list[dict[str, Any]] = []
    planned = [dict(m) for m in plan.get("medications", [])]
    used: set[int] = set()
    creatinine = find_observation(observations, "creatinine")
    kidney_low = bool(creatinine and (_value_number(creatinine["value"]) or 0) > KIDNEY_LIMIT)

    def plan_for(text: str) -> dict[str, Any] | None:
        for i, item in enumerate(planned):
            if i not in used and item["drug"].lower() in text.lower():
                used.add(i)
                return item
        return None

    def prescribe(item: dict[str, Any], action: str, name: str | None = None,
                  note: str = "", record: dict[str, Any] | None = None) -> None:
        """Add a medicine that needs a prescription, after the allergy check."""
        record = record or {}
        rx = {"drug": item["drug"].lower(), "dose": item.get("dose") or record.get("dose", ""),
              "quantity": item.get("quantity") or record.get("quantity", 30),
              "sig": item.get("sig") or record.get("sig", "as directed")}
        allergy = _allergy_for(rx["drug"], allergies)
        target = new_items if action == "new" else plan_list
        if allergy:
            kind = "antibiotic" if drug_class(rx["drug"]) in ANTIBIOTIC_CLASSES else "medicine"
            alt = ALTERNATIVES.get(rx["drug"])
            cls = drug_class(rx["drug"])
            substance = allergy.get("substance") or allergy.get("drugClass", "")
            detail = (f"The discharge plan {'starts' if action == 'new' else 'renews'} "
                      f"{rx['drug']}" + (f", a {cls}" if cls else "")
                      + f". The patient has a {substance.lower()} allergy "
                        f"({allergy.get('reaction', 'reaction not recorded').lower()}).")
            discrepancies.append({
                "severity": "high", "title": f"Planned {kind} conflicts with a documented allergy",
                "detail": detail,
                "suggestion": f"Use {_drug_name(alt)} ({alt['sig']}) instead." if alt
                              else "Choose an alternative: not prescribed."})
            if alt:
                target.append({"name": _drug_name(alt), "action": action, "prescription": dict(alt),
                               "note": f"replaces planned {rx['drug']}",
                               **({"controlled": CONTROLLED[alt["drug"]]}
                                  if alt["drug"] in CONTROLLED else {})})
            return
        entry = {"name": name or _drug_name(rx), "action": action, "prescription": rx,
                 "note": note or item.get("reason", "")}
        schedule = record.get("controlledSchedule") or CONTROLLED.get(rx["drug"])
        if schedule:
            entry["controlled"] = schedule
        target.append(entry)

    for med in meds:
        name = med["medication"]["text"]
        short = short_medication(name)
        labels = [x.get("code") for x in med.get("meta", {}).get("security", [])]
        if med.get("category") == "inpatient":
            plan_list.append({"name": short, "action": "stop",
                              "note": med.get("dischargeAction", "hospital only")
                              .removeprefix("stop ").strip("()") or "hospital only"})
            continue
        item = plan_for(name)
        action = (item or {}).get("action")
        if action == "stop":
            plan_list.append({"name": short, "action": "stop", "note": item.get("reason", "")})
            continue
        if med.get("status") == "on-hold":
            ok, evidence = _hold_check(med, observations)
            hold = med.get("holdReason", "held on admission")
            if ok:
                plan_list.append({"name": short, "action": "resume",
                                  "note": (item or {}).get("reason", ""), "check": evidence})
                discrepancies.append({
                    "severity": "medium", "title": f"{short} was held during the stay",
                    "detail": hold, "suggestion": f"Resume at home dose: {evidence}."})
            elif ok is False:
                plan_list.append({"name": short, "action": "hold", "note": hold,
                                  "check": evidence})
                discrepancies.append({
                    "severity": "high" if action == "resume" else "medium",
                    "title": f"Plan restarts {short}, but it should stay on hold"
                             if action == "resume" else f"{short} was held during the stay",
                    "detail": f"{hold}. Restart rule not met: {evidence}.",
                    "suggestion": "Keep holding; recheck before restarting."})
            else:
                plan_list.append({"name": short, "action": "resume" if action == "resume"
                                  else "hold", "note": hold})
                if action != "resume":
                    discrepancies.append({
                        "severity": "medium", "title": f"{short} was held during the stay",
                        "detail": hold, "suggestion": "The plan doesn't say: keep holding and "
                                                      "review at follow-up."})
            continue
        if action == "renew":
            # A new dose (e.g. a patient request the physician accepted) is named by what will
            # be prescribed, not by the home medicine's old strength.
            # Most records have no "dose" field: then the strength in the name ("10 MG").
            home = med.get("dose") or _strength(name)
            changed = bool(item.get("dose") and home) and item["dose"].lower() != home.lower()
            shown = f"{_drug_name(item)} (was {home})" if changed else short
            prescribe(item, "renew", name=shown, note=item.get("reason") or
                      med.get("supplyNote", ""), record=med)
            if plan_list and plan_list[-1]["name"] == shown:
                plan_list[-1]["source"] = med["id"]
                if not changed:
                    plan_list[-1]["note"] = med.get("supplyNote") or plan_list[-1]["note"]
            continue
        if kidney_low and drug_class(name.split()[0]) == "nsaid":
            discrepancies.append({
                "severity": "medium", "title": f"{short} continues with reduced kidney function",
                "detail": f"Creatinine {creatinine['value']}: NSAIDs can worsen kidney injury.",
                "suggestion": "Consider stopping it; use acetaminophen for pain."})
        plan_list.append({"name": short, "action": "continue", "source": med["id"],
                          "labels": labels})

    for i, item in enumerate(planned):
        if i in used:
            continue
        if item["action"] == "new":
            prescribe(item, "new")
        elif item["action"] in ("renew", "resume"):
            discrepancies.append({
                "severity": "medium",
                "title": f"Plan says {item['action']} {item['drug']}, but it is not on the "
                         "medication list",
                "detail": "No current or held prescription matches.",
                "suggestion": "Check the plan: nothing prescribed."})

    for eq in plan.get("equipment", []):
        plan_list.append({"name": eq["item"], "action": "new", "note": eq.get("detail", ""),
                          "dme": True, "prior_auth": bool(eq.get("prior_auth"))})
    discrepancies.sort(key=lambda d: d["severity"] != "high")   # most serious first
    return {"discrepancies": discrepancies, "discharge_medications": new_items + plan_list}


def compare_proposal(proposed: list[dict[str, Any]],
                     verified: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Model proposes, rules verify: where the model's discharge list differs from the
    rules' list, a finding for the physician. The rules' list is what goes ahead."""
    findings: list[dict[str, str]] = []
    meds = [m for m in verified if not m.get("dme")]
    matched: set[int] = set()
    for item in proposed:
        drug = str(item.get("drug", "")).lower().strip()
        if not drug:
            continue
        key = drug.split()[0].split("-")[0]
        index = next((i for i, m in enumerate(meds) if key in m["name"].lower()), None)
        if index is None:
            if item.get("action") in ("new", "renew", "resume", "continue"):
                findings.append({
                    "severity": "medium", "title": f"Model proposed {drug}: not in the "
                                                   "verified plan",
                    "detail": f"The model wanted to {item.get('action')} {drug}"
                              + (f" ({item['why']})" if item.get("why") else "") + ".",
                    "suggestion": "Not added. Review if it was intended."})
            continue
        matched.add(index)
        rules = meds[index]["action"]
        if item.get("action") != rules and {item.get("action"), rules} != {"renew",
                                                                              "continue"}:
            findings.append({
                "severity": "medium",
                "title": f"Model and rules disagree on {meds[index]['name']}",
                "detail": f"Model: {item.get('action')}"
                          + (f" ({item['why']})" if item.get("why") else "")
                          + f". Rules: {rules}.",
                "suggestion": f"Rules win ({rules}); change it yourself if you disagree."})
    for i, m in enumerate(meds):
        if i not in matched and m["action"] in ("new", "renew", "resume", "hold"):
            findings.append({
                "severity": "medium", "title": f"Model left out {m['name']}",
                "detail": f"The verified plan says {m['action']}.",
                "suggestion": "Kept from the verified plan."})
    return findings


def scheduling_preferences(messages: list[dict[str, Any]]) -> str:
    """The patient's portal messages, for the scheduling model (days and times they prefer).
    Untrusted text from outside: the model sees it, the boundaries don't care what it says."""
    if not messages:
        return "The patient has sent no portal messages."
    return ("The patient's portal messages (for scheduling preferences):\n"
            + "\n".join(f"- {m['text']}" for m in messages))


def follow_up_request(plan: dict[str, Any]) -> dict[str, Any]:
    """What to book, from the plan (primary care within 7 days if the plan doesn't say)."""
    f = plan.get("followUp") or {}
    return {"specialty": f.get("specialty", "primary care"),
            "within_days": int(f.get("within_days", 7)),
            "reason": f.get("reason", "Hospital follow-up"), "note": f.get("note", "")}


def follow_up_plan_text(plan: dict[str, Any]) -> str:
    f = follow_up_request(plan)
    text = f"{f['specialty'].capitalize()} within {f['within_days']} days"
    return text + (f"; {f['note']}" if f["note"] else "")


def equipment_lines(meds_plan: list[dict[str, Any]]) -> list[str]:
    return [f"- Use your {m['name'].lower()}: {m['note']}." if m.get("note")
            else f"- Use your {m['name'].lower()}." for m in meds_plan if m.get("dme")]


def reconciliation_report(result: dict[str, Any], withheld: list[dict[str, Any]]) -> str:
    lines = []
    for d in result["discrepancies"]:
        icon = "⚠" if d["severity"] == "high" else "•"
        lines.append(f"{icon} **{d['title']}.** {d['detail']} → {d['suggestion']}")
    groups: dict[str, list[str]] = {}
    for m in result["discharge_medications"]:
        groups.setdefault(m["action"], []).append(m["name"])
    labels = {"new": "New", "renew": "Renew", "resume": "Resume", "continue": "Continue",
              "stop": "Stop", "hold": "Hold"}
    for action in ("new", "renew", "resume", "continue", "stop", "hold"):
        if action in groups:
            lines.append(f"**{labels[action]}:** {', '.join(groups[action])}")
    if withheld:
        lines.append(withheld_line(withheld))
    return "\n".join(lines)


# --------------------------------------------------------------------------- discharge summary


def discharge_summary(summary: dict[str, Any], meds_plan: list[dict[str, Any]],
                      observations: list[dict[str, Any]], notes: list[dict[str, Any]],
                      follow_up: str | None) -> dict[str, Any]:
    """A structured summary. Each section names the record it came from (``source``), so the
    disclosure gateway can withhold sections drawn from sensitive records."""
    p, stay = summary["patient"], summary.get("admission") or {}
    plan = stay.get("dischargePlan", {})
    progress = next((n for n in notes if n["type"]["text"].startswith("Progress note")), None)

    problems = [c for c in summary["active_conditions"] if not sensitive(c)]
    routine_meds = [m for m in meds_plan if not sensitive(m)]
    sections = [
        {"title": "Admission", "text": f"Admitted {stay.get('period', {}).get('start', '')[:10]} "
                                       f"with {stay.get('reason', 'unknown reason').lower()}.",
         "source": None},
        {"title": "Hospital course",
         "text": progress["text"] if progress else "See chart.",
         "source": progress["id"] if progress else None},
        {"title": "Discharge diagnoses",
         "text": "; ".join(short_condition(c["code"]["text"]) for c in problems[:6]),
         "source": None},
        {"title": "Discharge medications",
         "text": "; ".join(f"{m['action']} {m['name']}" for m in routine_meds), "source": None},
    ]
    # Sensitive items never go in a general section: each gets its own section, sourced to its
    # record, so the disclosure gateway can withhold it without consent.
    for condition in summary["active_conditions"]:
        if sensitive(condition):
            sections.append({"title": "Behavioral health",
                             "text": short_condition(condition["code"]["text"]),
                             "source": condition["id"]})
    for med in meds_plan:
        if sensitive(med):
            sections.append({"title": "Behavioral health medication",
                             "text": f"{med['action']} {med['name']}", "source": med["source"]})
    key = key_results(observations)
    if key:
        sections.append({"title": "Key results",
                         "text": "; ".join(f"{o['code']['text']}: {o['value']}" for o in key),
                         "source": None})
    sections.append({"title": "Follow-up",
                     "text": follow_up or follow_up_plan_text(plan), "source": None})
    return {"patient": {"name": p["name"], "dob": p["birthDate"]},
            "discharge_date": plan.get("readyDate"), "attending": stay.get("attending"),
            "sections": sections}


def key_results(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Results from this stay that someone interpreted (e.g. "low: qualifies for oxygen")."""
    return [o for o in observations if o.get("interpretation")
            and (o.get("effectiveDateTime") or "") >= "2026"][:4]


def sensitive(item: dict[str, Any]) -> bool:
    """Labelled (PSY, HIV, ...) in the chart or in the medication plan."""
    return bool(item.get("labels") or item.get("meta", {}).get("security"))


def hospital_course_facts(summary: dict[str, Any], meds_plan: list[dict[str, Any]],
                          observations: list[dict[str, Any]], notes: list[dict[str, Any]],
                          follow_up: str | None) -> str:
    """What the model may use for the "Hospital course" paragraph. Sensitive records are left
    out *before* the model sees anything, so its text can never leak them."""
    stay = summary.get("admission") or {}
    progress = next((n for n in notes if n["type"]["text"].startswith("Progress note")
                     and not sensitive(n)), None)
    lines = [f"Patient: {summary['patient']['name']}, {summary['patient'].get('age')} years",
             f"Admitted: {stay.get('period', {}).get('start', '')[:10]} with "
             f"{stay.get('reason', 'unknown reason')}",
             "Active problems: " + "; ".join(short_condition(c["code"]["text"])
                                             for c in summary["active_conditions"]
                                             if not sensitive(c))]
    if progress:
        lines.append(f"Latest progress note: {progress['text']}")
    lines += [f"{o['code']['text']}: {o['value']}" for o in key_results(observations)]
    lines.append("Discharge medication plan: " + "; ".join(
        f"{m['action']} {m['name']}" + (f" ({m['note']})" if m.get("note") else "")
        for m in meds_plan if not sensitive(m)))
    lines.append(f"Follow-up: {follow_up or follow_up_plan_text(stay.get('dischargePlan', {}))}")
    return "\n".join(lines)


def split_messages(messages: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """(safe, quarantined): portal messages that look like prompt injection are kept away from
    the model and reported instead."""
    safe = [m for m in messages if not looks_like_injection(m["text"])]
    return safe, [m for m in messages if looks_like_injection(m["text"])]


def instructions_facts(name: str, meds_plan: list[dict[str, Any]], follow_up: str | None,
                       messages: list[dict[str, Any]], pharmacy: str | None,
                       warning_signs: str = DEFAULT_WARNING_SIGNS) -> str:
    """What the model may use for the patient's instructions (``messages`` already screened)."""
    changed = [m["name"] for m in meds_plan
               if m["action"] in ("new", "renew", "resume") and not sensitive(m)]
    lines = [f"Patient first name: {name.split()[0]}",
             f"Medicine changes (the app lists them exactly; do not list them): "
             f"{len(changed)}"]
    lines.append("Prescriptions: go to the pharmacy after the doctor signs them")
    lines += [f"Equipment: {m['name']} ({m.get('note') or 'as shown by the care team'})"
              for m in meds_plan if m.get("dme")]
    lines += [f"Follow-up: {follow_up or 'family doctor within 7 days'}",
              f"Pharmacy: {pharmacy or 'not chosen yet'}",
              f"Warning signs: {warning_signs}: call us or 911"]
    for m in messages:
        lines.append(f"Portal message from the patient ({m['sent'][:10]}): <message>{m['text']}"
                     "</message>")
    return "\n".join(lines)


def summary_preview(summary: dict[str, Any]) -> str:
    return "\n".join(f"**{s['title']}:** {first_sentence(s['text'], 220)}"
                     for s in summary["sections"])


# --------------------------------------------------------------------------- patient instructions


def medicine_lines(meds_plan: list[dict[str, Any]]) -> list[str]:
    """The medicine part of the patient's instructions: always from the reconciled plan,
    never from a model (a model may drop, merge or change a dose)."""
    lines = []
    for m in meds_plan:
        if m["action"] in ("new", "renew", "resume") and not m.get("dme"):
            note = f" ({m['note']})" if m.get("note") else ""
            verb = {"new": "Start", "renew": "Keep taking", "resume": "Restart"}[m["action"]]
            lines.append(f"- {verb} {m['name']}{note}.")
    return lines


def patient_instructions(name: str, meds_plan: list[dict[str, Any]],
                         follow_up: str | None, messages: list[dict[str, Any]],
                         pharmacy: str | None,
                         warning_signs: str = DEFAULT_WARNING_SIGNS) -> tuple[str, list[str]]:
    """Plain-language instructions; answers questions from portal messages; flags injections."""
    first = name.split()[0]
    lines = [f"Hi {first}, here is what to do at home:", *medicine_lines(meds_plan)]
    lines += equipment_lines(meds_plan)
    if follow_up:
        lines.append(f"- Follow-up visit: {follow_up}.")
    lines.append(f"- Call us or 911 if you have {warning_signs}.")
    warnings = []
    for msg in messages:
        if looks_like_injection(msg["text"]):
            warnings.append(f"portal message of {msg['sent'][:16].replace('T', ' ')} contains "
                            "instructions addressed to AI assistants")
    if pharmacy:
        lines.append(f"Your prescriptions are going to {pharmacy}.")
    return "\n".join(lines), warnings


# --------------------------------------------------------------------------- benefits / scheduling


def registry_line(response: dict[str, Any]) -> str:
    """One line of evidence from the state controlled-substance registry."""
    last = response.get("last_fill") or {}
    fills = (f"last fill {last.get('date')} ({last.get('quantity')} tablets)" if last
             else "no fills in 12 months")
    flags = ", ".join(response.get("risk_flags") or []) or "no risk flags"
    return (f"NC registry: {response.get('prescribers', 0)} prescriber(s), "
            f"{response.get('pharmacies', 0)} pharmacy(ies), {fills}; {flags}")


def coverage_line(coverage: dict[str, Any] | None) -> str:
    c = coverage or {}
    return f"{c.get('payor', 'No coverage on file')} {c.get('plan', '')} ({c.get('status', '?')})"


def follow_up_text(appointment: dict[str, Any]) -> str:
    return f"{appointment.get('clinician')}, {appointment.get('when')}"


# --------------------------------------------------------------------------- chart Q&A


def plan_text(plan: dict[str, Any]) -> str:
    """The discharge plan as written, in one line (for the chart and chat)."""
    parts = [f"{m['action']} {m['drug']}" + (f" {m['dose']}" if m.get("dose") else "")
             for m in plan.get("medications", [])]
    parts += [f"{e['item']} ({e['detail']})" for e in plan.get("equipment", [])]
    if plan.get("followUp"):
        parts.append(follow_up_plan_text(plan))
    return "; ".join(parts) or "not written yet"


TOPICS = (
    ("allerg", "allergies"), ("medic|meds|drug|prescri", "medications"),
    ("oxygen|saturation|spo2|lab|creatinine|wbc|vital|result", "observations"),
    ("note|psych|history|depress", "notes"), ("why|admit|admission|reason|stay", "admission"),
    ("problem|diagnos|condition", "problems"),
)


def topic_of(question: str) -> str:
    for pattern, topic in TOPICS:
        if re.search(pattern, question, re.IGNORECASE):
            return topic
    return "overview"


def answer(topic: str, data: dict[str, Any]) -> str:
    withheld = data.get("withheld", [])
    if topic == "allergies":
        items = [f"{a['substance']} ({a.get('reaction', '?')}, {a.get('severity', '?')})"
                 for a in data["allergies"]] or ["none documented"]
        body = "**Allergies:** " + "; ".join(items)
    elif topic == "medications":
        body = "**Medications:**\n" + "\n".join(
            f"- {short_medication(m['medication']['text'])} · {m.get('category', 'home')}"
            + (" · held" if m.get("status") == "on-hold" else "")
            + (f" · {m['controlledSchedule']}" if m.get("controlledSchedule") else "")
            for m in data["medications"])
    elif topic == "observations":
        body = "**Latest results:**\n" + "\n".join(
            f"- {o['code']['text']}: {o['value']}"
            + (f" ({o['interpretation']})" if o.get("interpretation") else "")
            for o in data["observations"][:8])
    elif topic == "notes":
        body = "**Notes:**\n" + "\n".join(
            f"- {n['date']} {n['type']['text']}: {first_sentence(n['text'])}"
            for n in data["notes"][:5])
    elif topic == "admission":
        stay = data.get("admission") or {}
        body = (f"**Admitted** {stay.get('period', {}).get('start', '')[:10]} for "
                f"{stay.get('reason', '?').lower()}. **Plan:** "
                f"{plan_text(stay.get('dischargePlan', {}))}.")
    else:
        conditions = [short_condition(c["code"]["text"]) for c in data["active_conditions"]]
        body = "**Active problems:** " + ", ".join(conditions[:8])
    return body + ("\n" + withheld_line(withheld) if withheld else "")


# --------------------------------------------------------------------------- patient portal

REQUEST_KINDS = ("medication_change", "refill", "side_effect", "question")
DOSE = re.compile(r"\b(\d+(?:\.\d+)?)\s?(mg|mcg|g|units?)\b", re.IGNORECASE)
REFILL = re.compile(r"\brefill|run(ning)? (out|low)|ran out|more (pills|tablets)", re.IGNORECASE)
CHANGE = re.compile(r"\b(increase|raise|higher|lower|decrease|reduce|stop|switch|change|"
                    r"double|dose)\b", re.IGNORECASE)
SIDE_EFFECT = re.compile(r"side effect|dizz|nause|rash|itch|drows|sleepy|constipat|vomit",
                         re.IGNORECASE)
PATIENT_REPLIES = {
    "medication_change": "Thanks, {first}. Your request was sent to Dr. Pendragon's team. A doctor "
                         "will review it; nothing changes until they decide.",
    "refill": "Thanks, {first}. Your refill request was sent to your care team for review.",
    "side_effect": "Thanks for telling us, {first}. Your care team has been alerted. If you "
                   "have trouble breathing, chest pain or swelling of the face, call 911.",
    "question": "Thanks, {first}. Your message was sent to your care team; they will reply "
                "here.",
}


def medicine_names(meds: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{'oxycodone': record, 'metformin': record, ...}: the generic name a patient would use."""
    names = {}
    for med in meds:
        words = [w for w in re.split(r"[\s/]+", med["medication"]["text"].lower())
                 if w.isalpha() and len(w) > 3 and w not in ("oral", "tablet", "extended",
                                                              "release", "hydrochloride")]
        if words:
            names[words[0]] = med
    return names


def triage(message: str, meds: list[dict[str, Any]]) -> dict[str, Any]:
    """Rules: what kind of request is this, about which medicine, and is it suspicious."""
    names = medicine_names(meds)
    lowered = message.lower()
    drug = next((n for n in names if n in lowered), None)
    dose = DOSE.search(message)
    if SIDE_EFFECT.search(message):
        kind = "side_effect"
    elif REFILL.search(message) and drug:
        kind = "refill"
    elif CHANGE.search(message) and drug:
        kind = "medication_change"
    else:
        kind = "question"
    return {"kind": kind, "drug": drug,
            "requested_dose": f"{dose.group(1)} {dose.group(2).lower()}" if dose else None,
            "summary": first_sentence(message, 160),
            "flags": ["prompt_injection"] if looks_like_injection(message) else []}


def verify_triage(proposed: dict[str, Any], rules: dict[str, Any],
                  meds: list[dict[str, Any]]) -> dict[str, Any]:
    """Model proposes, rules verify: only known kinds; a medicine must be on her list; a
    suspicious message stays flagged whatever the model says."""
    names = medicine_names(meds)
    kind = proposed.get("kind") if proposed.get("kind") in REQUEST_KINDS else rules["kind"]
    drug = str(proposed.get("drug") or "").lower().strip() or None
    if drug not in names:          # unknown or missing: the rules' reading of her list
        drug = rules["drug"]
    if kind in ("medication_change", "refill") and not drug:
        kind = "question"
    dose = proposed.get("requested_dose") or rules["requested_dose"]
    if dose and not DOSE.fullmatch(str(dose).strip()):
        dose = rules["requested_dose"]
    summary = str(proposed.get("summary") or "").strip()[:160] or rules["summary"]
    return {"kind": kind, "drug": drug, "requested_dose": dose, "summary": summary,
            "flags": rules["flags"]}
