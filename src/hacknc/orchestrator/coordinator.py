"""The discharge coordinator as a Strands agent: one chat message in, one reply out.

The physician (or nurse) types anything: "Summarize Mr. Lynch", "Discharge Jane Doe",
"What's still open?". The model decides whether to answer or to orchestrate, which
specialist to call next, and what findings to pass on (e.g. an allergy to the prescription
agent). Its tools are the specialists, each reached exactly like the rules checklist reaches
them: a token narrowed to that agent, then its MCP tool.

What the model can *not* do, because it is enforced in code, not asked for in the prompt:

* order a discharge without the user's ``discharge:order`` (Keycloak grants it to physicians)
* run a task before its prerequisites (``run_task`` checks dependencies)
* change the verified medication plan (tools take no plan argument; it comes from medrec)
* finish early: ``record_discharge`` is only offered once every draft is signed and sent
  (``ready_to_record``), and checked again when called

When the model is off or unreachable, ``rules_reply`` handles the message with the fixed
checklist instead, so the demo works without Wi-Fi.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from strands import tool

from hacknc.common.auth import TokenClaims
from hacknc.orchestrator.discharge import AlreadyStarted, Discharge, DischargeBoard, Task

SYSTEM = """You are the discharge coordinator on hospital ward 4 West. You talk with \
{user} ({role}) about patient "{patient_id}". You hold no patient data yourself: specialist \
agents do the work through your tools.

- A question about the patient: call answer_question with the question, and reply with its \
answer.
- An instruction to discharge: call start_discharge first. If it is refused, explain why and \
stop. Then call the specialists, in an order that respects their prerequisites: \
reconcile_medications first; arrange_equipment and book_follow_up are independent; \
prepare_prescriptions and draft_discharge_summary need reconciliation; \
write_patient_instructions needs reconciliation and the follow-up booking. Pass important \
findings on as notes (for example an allergy finding to prepare_prescriptions).
- "What's left?" or similar: call discharge_status.
- If a task comes back suspended for a security alert, say so plainly: the gateway blocked \
that agent, the discharge is on hold, and only the physician can review it in the app. Do not \
retry a suspended task.
- Never invent results; report what the tools returned. Prescriptions and the summary wait \
for the physician's signature in the app: say so.
- Reply in at most 6 short markdown bullet points."""


@dataclass
class Turn:
    """One chat message being handled."""

    board: DischargeBoard
    patient_id: str
    claims: TokenClaims
    user_token: str
    run_task: Callable[..., Awaitable[Task]]
    ask: Callable[[str], Awaitable[str]]
    start: Callable[[], str | None]      # returns a refusal, or None when started

    @property
    def discharge(self) -> Discharge:
        return self.board.get(self.patient_id)


def task_line(task: Task) -> str:
    line = f"{task.title}: {task.status.replace('_', ' ')}"
    if task.summary:
        line += f" ({task.summary})"
    return line


def status_text(d: Discharge) -> str:
    if d.status == "not_started":
        return "No discharge under way."
    return f"Discharge {d.status.replace('_', ' ')}.\n" + "\n".join(
        f"- {task_line(t)}" for t in d.tasks)


def ready_to_record(d: Discharge) -> tuple[bool, str]:
    """The readiness guard. True only when everything that needed a physician is signed
    and has gone out. Never decided by a model."""
    if d.task("medrec").status != "done":
        return False, "medication reconciliation is not done"
    for task_id in ("prescriptions", "summary"):
        task = d.task(task_id)
        if task.status == "disabled":
            continue
        if task.status != "done":
            return False, f"{task.title.lower()} is {task.status.replace('_', ' ')}"
        if any(s["status"] not in ("sent", "rejected", "replaced", "manual")
               for s in task.signatures):
            return False, f"{task.title.lower()} has items not signed and sent"
    if d.status != "completed":
        return False, "sending to the pharmacy and family doctor has not finished"
    return True, "every signed item has gone out"


def tools_for(turn: Turn) -> list[Any]:
    """The coordinator model's toolbox for this message."""

    def outcome(task: Task) -> str:
        return f"{task_line(task)}\n{task.detail}".strip()

    async def specialist(task_id: str, notes: str = "") -> str:
        d = turn.discharge
        if d.status == "not_started":
            return "Refused: no discharge under way. Call start_discharge first."
        if d.status == "completed":
            return "Refused: this discharge is complete."
        if d.status == "on_hold" and d.task(task_id).status == "suspended":
            return "Refused: this task is suspended after a security alert; the physician " \
                   "must review it in the app."
        return outcome(await turn.run_task(d, task_id, turn.user_token, notes=notes))

    @tool
    async def answer_question(question: str) -> str:
        """Ask the chart agent a question about the patient (it sees only what the user may)."""
        return await turn.ask(question)

    @tool
    def start_discharge() -> str:
        """Start the discharge (needs the user's discharge:order permission)."""
        refusal = turn.start()
        return f"Refused: {refusal}" if refusal else "Discharge started."

    @tool
    async def reconcile_medications() -> str:
        """Medication reconciliation agent: verified discharge medicine list and findings."""
        return await specialist("medrec")

    @tool
    async def arrange_equipment() -> str:
        """Benefits agent: coverage and prior authorization for home equipment."""
        return await specialist("benefits")

    @tool
    async def book_follow_up() -> str:
        """Scheduling agent: books the follow-up visit from the discharge plan."""
        return await specialist("scheduling")

    @tool
    async def prepare_prescriptions(notes: str = "") -> str:
        """Prescription agent: drafts prescriptions from the verified plan for signature.

        Args:
            notes: findings from other agents it should take into account.
        """
        return await specialist("prescriptions", notes)

    @tool
    async def draft_discharge_summary() -> str:
        """Summary agent: drafts the discharge summary for the physician's signature."""
        return await specialist("summary")

    @tool
    async def write_patient_instructions() -> str:
        """Instructions agent: writes and posts plain-language patient instructions."""
        return await specialist("instructions")

    @tool
    def discharge_status() -> str:
        """Where the discharge stands: every task and what it is waiting for."""
        return status_text(turn.discharge)

    tools = [answer_question, start_discharge, reconcile_medications, arrange_equipment,
             book_follow_up, prepare_prescriptions, draft_discharge_summary,
             write_patient_instructions, discharge_status]
    ready, _ = ready_to_record(turn.discharge)
    if ready:   # offered only when the guard says so; the guard runs again on the call
        @tool
        def record_discharge() -> str:
            """Record in the EHR that the patient has left the hospital."""
            ok, why = ready_to_record(turn.discharge)
            return ("Ready: the discharge is recorded in the EHR as this turn ends."
                    if ok else f"Refused: {why}")

        tools.append(record_discharge)
    return tools


# A short fragment glued in front of the first bullet ("Need to explain.- **Discharge...").
STRAY_PREFIX = re.compile(r"\A[^\n*#-]{1,60}?[.:](?=[ \t]*- )")


def tidy_reply(text: str) -> str:
    """The reply is asked for as bullet points. gpt-oss sometimes lets a scrap of its own
    reasoning through in front of the first one: drop it, and change nothing else."""
    return STRAY_PREFIX.sub("", text, count=1).lstrip()


DISCHARGE = re.compile(r"\bdischarg", re.IGNORECASE)
STATUS = re.compile(r"\b(status|left|open|remaining|pending|progress)\b", re.IGNORECASE)


async def rules_reply(turn: Turn, message: str,
                      run_checklist: Callable[[Discharge], Awaitable[None]]) -> str:
    """No model: keywords decide. 'discharge' starts the fixed checklist; 'status' reports;
    anything else goes to the chart agent."""
    if DISCHARGE.search(message) and not STATUS.search(message):
        refusal = turn.start()
        if refusal:
            return f"🛑 {refusal}"
        await run_checklist(turn.discharge)
        return status_text(turn.discharge)
    if STATUS.search(message):
        return status_text(turn.discharge)
    return await turn.ask(message)


def start_refusal(board: DischargeBoard, patient_id: str, claims: TokenClaims,
                  already: Callable[[Discharge], str]) -> str | None:
    if "discharge:order" not in claims.scopes:
        return (f"{claims.username} may not order a discharge: the token has no "
                "discharge:order (Keycloak only grants it to physicians)")
    try:
        board.start(patient_id, claims.username)
    except AlreadyStarted as exc:
        if exc.discharge.status in ("in_progress", "awaiting_signatures", "on_hold"):
            return None   # carry on with the discharge already under way
        return already(exc.discharge)
    return None
