# Minimal stand-in for crewai so the real app code can be imported and exercised
# without the 119-package dependency tree. Only the AI calls are fake.
class _Base:
    def __init__(self, **kw):
        self.kw = kw
        self.__dict__.update(kw)


class Agent(_Base):
    pass


class Task(_Base):
    pass


class LLM(_Base):
    def call(self, prompt):
        return "[]"


class Crew:
    def __init__(self, agents=None, tasks=None, verbose=False):
        self.tasks = tasks or []

    def kickoff(self):
        return "stub"
