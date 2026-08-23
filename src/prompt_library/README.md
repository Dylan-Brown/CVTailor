# src/prompt_library/

Every system prompt the pipeline uses, as plain `.txt` files instead of
triple-quoted string constants buried inside stage scripts -- editable,
reviewable, and diffable on their own.

* **`py_prompt_template_handler.py`** — the one loader every stage uses to
  read a prompt file, so every stage loads prompts the same way.
* **`stages/`** — one subfolder per stage that needs one, named to match
  (`stages/stage05_propose_updates/gen_system.txt` backs
  `src/stages/py_stage05_propose_updates.py`, etc.) -- always obvious
  which prompt belongs to which script.
* **`util/`** — prompts for `src/util/` scripts (currently just
  `py_answer_question.py`'s system prompt).
