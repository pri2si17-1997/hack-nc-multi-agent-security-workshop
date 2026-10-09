from types import SimpleNamespace

from hacknc.agents.servers import AgentRun


class Span:
    def __init__(self) -> None:
        self.events = []

    def add_event(self, name, attributes=None) -> None:
        self.events.append(name)


def run_with(on: set[str]):
    audited = []
    deps = SimpleNamespace(scenario=lambda name: name in on)
    run = AgentRun(deps, "scheduling-agent", "book_follow_up", "pt-1")
    run.span = Span()
    run.audit = lambda action, decision, reason, details=None, severity="info": \
        audited.append((action, reason, details))
    return run, audited


def test_a_staged_attack_is_marked_once_per_run() -> None:
    run, audited = run_with({"prompt_injection"})
    assert run.scenario("prompt_injection") and run.scenario("prompt_injection")
    assert len(audited) == 1
    action, reason, details = audited[0]
    assert action == "scenario.staged" and "Prompt injection" in reason
    assert details == {"scenario": "prompt_injection", "attack_style": "Prompt injection"}


def test_nothing_staged_nothing_marked() -> None:
    run, audited = run_with(set())
    assert not run.scenario("prompt_injection")
    assert audited == [] and run.span.events == []
