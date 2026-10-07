"""The first-run notice voicebox shows before it collects or sends anything. Whether it
is owed is speak-core's `telemetry.notice`; the words, and where they appear, are
voicebox's: appended to the first tool reply of a session, and printed by
`/voicebox:setup`. A tool reply reaches the model, not the user, so it carries an
instruction to show the notice word for word."""

from collections.abc import Callable

TELEMETRY_URL = "https://github.com/danilogr/voicebox/blob/main/TELEMETRY.md"

TEXT = f"""voicebox collects anonymous usage data: which features you use, how long jobs
take on your machine, why they fail, and the size and format of files (never
their names or contents, and never the text you synthesize).

  /voicebox:telemetry off     collect nothing; send only an anonymous install count
  /voicebox:telemetry local   keep the data; send only an anonymous install count
  /voicebox:stats             see your own

Details: {TELEMETRY_URL}"""

FOR_THE_MODEL = "voicebox's one-time telemetry notice. Show it to the user word for word, once:"


class Notice:
    """The notice for one session: handed out at most once, and marked shown when it is."""

    def __init__(self, *, pending: bool, mark_shown: Callable[[], object]) -> None:
        self._pending = pending
        self._mark_shown = mark_shown

    def take(self) -> str | None:
        if not self._pending:
            return None
        self._pending = False
        self._mark_shown()
        return f"{FOR_THE_MODEL}\n\n{TEXT}"
