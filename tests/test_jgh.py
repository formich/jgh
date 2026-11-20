import importlib.util
import os
import tempfile
import unittest

os.environ["JGH_HOME"] = tempfile.mkdtemp()
_spec = importlib.util.spec_from_file_location(
    "jgh", os.path.join(os.path.dirname(__file__), "..", "jgh.py")
)
jgh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jgh)

CFG = dict(jgh.DEFAULT_CONFIG, project_keys=["PROJ"])


def pr(state, base="main", default="main", number=1):
    return {"ref": f"o/r#{number}", "repo": "o/r", "number": number, "state": state,
            "base": base, "default_branch": default, "draft": False}


def issue(status, no_code=False, type="Bug", target=None):
    i = {"key": "PROJ-1", "status": status, "no_code": no_code, "type": type}
    if target:
        i["target"] = target
    return i


STORY_DONE = dict(CFG, target_status_by_type={"Story": "Done"})


class ExtractKey(unittest.TestCase):
    def test_project_keys(self):
        for branch in ["PROJ-12/fix", "PROJ-12-fix", "PROJ-12_fix", "feature/PROJ-12-x", "proj-12", "PROJ-12"]:
            self.assertEqual(jgh.extract_key(branch, CFG), "PROJ-12", branch)
        for branch in ["OTHER-12/fix", "PROJ-12abc", "main", "PROJ-0/x", "PROJ-000-x"]:
            self.assertIsNone(jgh.extract_key(branch, CFG), branch)

    def test_any_key(self):
        cfg = jgh.DEFAULT_CONFIG
        self.assertEqual(jgh.extract_key("ABC-7-thing", cfg), "ABC-7")
        self.assertIsNone(jgh.extract_key("fix/utf-8-bug", cfg))


class Classify(unittest.TestCase):
    def test_ready_when_all_merged_into_default(self):
        self.assertEqual(jgh.classify(issue("In Progress"), [pr("MERGED")], CFG), "move")

    def test_merge_into_other_branch_is_not_done(self):
        self.assertEqual(jgh.classify(issue("In Progress"), [pr("MERGED", base="dev")], CFG), "wip")

    def test_all_prs_must_be_merged(self):
        prs = [pr("MERGED", number=1), pr("OPEN", number=2)]
        self.assertEqual(jgh.classify(issue("In Review"), prs, CFG), "wip")

    def test_closed_unmerged_prs_are_ignored(self):
        prs = [pr("MERGED", number=1), pr("CLOSED", number=2)]
        self.assertEqual(jgh.classify(issue("In Progress"), prs, CFG), "move")

    def test_no_pr_is_wip(self):
        self.assertEqual(jgh.classify(issue("To Do"), [], CFG), "wip")

    def test_handed_over_with_open_pr_warns(self):
        prs = [pr("MERGED", number=1), pr("OPEN", number=2)]
        self.assertEqual(jgh.classify(issue("Testing"), prs, CFG), "warn_open")

    def test_target_without_pr_warns(self):
        self.assertEqual(jgh.classify(issue("Testing"), [], CFG), "warn_noprs")
        self.assertEqual(jgh.classify(issue("Done"), [], CFG), "ok")

    def test_no_code(self):
        self.assertEqual(jgh.classify(issue("Testing", no_code=True), [], CFG), "ok")
        self.assertEqual(jgh.classify(issue("In Progress", no_code=True), [], CFG), "nocode")
        self.assertEqual(jgh.classify(issue("In Progress", no_code=True), [pr("MERGED")], CFG), "move")


class Targets(unittest.TestCase):
    def test_target_resolution(self):
        self.assertEqual(jgh.target_of(issue("To Do"), STORY_DONE), "Testing")
        self.assertEqual(jgh.target_of(issue("To Do", type="Story"), STORY_DONE), "Done")
        self.assertEqual(jgh.target_of(issue("To Do", target="Done"), CFG), "Done")

    def test_story_in_testing_is_still_owned(self):
        story = issue("Testing", type="Story")
        self.assertEqual(jgh.classify(story, [pr("MERGED")], STORY_DONE), "move")
        self.assertEqual(jgh.classify(story, [pr("OPEN")], STORY_DONE), "wip")
        self.assertEqual(jgh.classify(issue("Done", type="Story"), [pr("MERGED")], STORY_DONE), "ok")
        self.assertEqual(jgh.classify(issue("Done", type="Story"), [], STORY_DONE), "ok")

    def test_bug_unchanged(self):
        self.assertEqual(jgh.classify(issue("Testing"), [pr("MERGED")], STORY_DONE), "ok")
        self.assertEqual(jgh.classify(issue("Testing"), [], STORY_DONE), "warn_noprs")

    def test_no_code_story(self):
        story = issue("Testing", type="Story", no_code=True)
        self.assertEqual(jgh.classify(story, [], STORY_DONE), "nocode")

    def test_jql_includes_extra_statuses_per_type(self):
        jql = jgh.my_issues_jql(STORY_DONE)
        self.assertIn('(issuetype in ("Story") AND status in ("Testing"))', jql)


class Workflow(unittest.TestCase):
    def test_plan_hops(self):
        self.assertEqual(jgh.plan_hops("In Progress", CFG), ["In Review", "Testing"])
        self.assertEqual(jgh.plan_hops("Blocked", CFG), ["Testing"])
        self.assertEqual(jgh.plan_hops("In Progress", CFG, "Done"), ["In Review", "Testing", "Done"])

    def test_jql(self):
        jql = jgh.my_issues_jql(CFG)
        self.assertIn('project in ("PROJ")', jql)
        self.assertIn('status in ("To Do", "In Progress", "In Review")', jql)
        self.assertNotIn("issuetype", jql)
        self.assertNotIn("project in", jgh.my_issues_jql(jgh.DEFAULT_CONFIG))


if __name__ == "__main__":
    unittest.main()
