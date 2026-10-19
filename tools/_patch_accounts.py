# -*- coding: utf-8 -*-
"""Патчит planning.py и assistant.py: поддержка нескольких аккаунтов."""

import io
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLANNING = os.path.join(ROOT, "planner", "planning.py")
ASSISTANT = os.path.join(ROOT, "planner", "assistant.py")

PLANNING_REPLACEMENTS = (
    ("    def __init__(self, store, feed, today=None):\n"
     "        self.store = store\n"
     "        self.feed = feed\n"
     "        self._today = today\n",
     "    def __init__(self, store, feed, today=None, account_id=1):\n"
     "        self.store = store\n"
     "        self.feed = feed\n"
     "        self._today = today\n"
     "        self.account_id = int(account_id or 1)\n"),
    ("        raw = self.store.settings()", "        raw = self.store.settings(self.account_id)"),
    ("            for step in self.store.all_subtasks(only_planned=True):",
     "            for step in self.store.all_subtasks(self.account_id, only_planned=True):"),
    ("        for task in self.store.tasks():",
     "        for task in self.store.tasks(self.account_id):"),
    ("            steps = self.store.subtasks(task[\"id\"])",
     "            steps = self.store.subtasks(task[\"id\"], self.account_id)"),
    ("                    self.store.add_subtask(task[\"id\"],\n"
     "                                           \"%s (%d/%d)\" % (step[\"title\"], index, total),\n"
     "                                           chunk, ord_value=int(step[\"ord\"]) + index - 1)",
     "                    self.store.add_subtask(task[\"id\"],\n"
     "                                           \"%s (%d/%d)\" % (step[\"title\"], index, total),\n"
     "                                           chunk, ord_value=int(step[\"ord\"]) + index - 1,\n"
     "                                           account_id=self.account_id)"),
    ("        if persist:\n            self.store.clear_plan()",
     "        if persist:\n            self.store.clear_plan(self.account_id)"),
    ("        tasks = [task for task in self.store.tasks() if not task[\"done\"]]",
     "        tasks = [task for task in self.store.tasks(self.account_id)\n"
     "                 if not task[\"done\"]]"),
    ("            steps = [step for step in self.store.subtasks(task[\"id\"]) if not step[\"done\"]]",
     "            steps = [step for step in self.store.subtasks(task[\"id\"],\n"
     "                                                        self.account_id)\n"
     "                     if not step[\"done\"]]"),
    ("                    self.store.plan_subtask(step[\"id\"], day_iso,\n"
     "                                            format_hhmm(start), format_hhmm(end))",
     "                    self.store.plan_subtask(step[\"id\"], day_iso,\n"
     "                                            format_hhmm(start), format_hhmm(end),\n"
     "                                            account_id=self.account_id)"),
    ("        planned = self.store.all_subtasks(only_planned=True)",
     "        planned = self.store.all_subtasks(self.account_id, only_planned=True)"),
)

ASSISTANT_REPLACEMENTS = (
    ("    def __init__(self, store, planner, feed=None, logger=print):\n"
     "        self.store = store\n"
     "        self.planner = planner\n"
     "        self.feed = feed\n"
     "        self.logger = logger if callable(logger) else (lambda message: None)\n",
     "    def __init__(self, store, planner, feed=None, logger=print):\n"
     "        self.store = store\n"
     "        self.planner = planner\n"
     "        self.feed = feed\n"
     "        self.logger = logger if callable(logger) else (lambda message: None)\n"
     "        # Всё, что делает ассистент, происходит в рамках одного аккаунта.\n"
     "        self.account_id = int(getattr(planner, \"account_id\", 1) or 1)\n"),
    ("        task_id = self.store.add_task(\n"
     "            title=title, subject=subject, kind=kind, deadline=deadline,\n"
     "            priority=priority,",
     "        task_id = self.store.add_task(\n"
     "            account_id=self.account_id,\n"
     "            title=title, subject=subject, kind=kind, deadline=deadline,\n"
     "            priority=priority,"),
    ("            self.store.add_subtask(task_id, label, minutes, index)",
     "            self.store.add_subtask(task_id, label, minutes, index,\n"
     "                                   account_id=self.account_id)"),
    ("        counts = self.store.counts()", "        counts = self.store.counts(self.account_id)"),
)


def patch(path, replacements):
    text = io.open(path, encoding="utf-8").read()
    missed = []
    for before, after in replacements:
        if before not in text:
            missed.append(before.strip().split("\n")[0][:70])
            continue
        text = text.replace(before, after)
    io.open(path, "w", encoding="utf-8").write(text)
    print("%s: замен %d, не найдено %d" % (os.path.basename(path),
                                           len(replacements) - len(missed), len(missed)))
    for item in missed:
        print("   не найдено: %s" % item)


def main():
    patch(PLANNING, PLANNING_REPLACEMENTS)
    patch(ASSISTANT, ASSISTANT_REPLACEMENTS)


if __name__ == "__main__":
    main()
