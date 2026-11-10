import unittest

from aboard.models import (
    KNOWLEDGE,
    SKILL,
    TASK,
    all_template,
    fields,
    id_prefix,
    next_id,
    parse_index,
    search_template,
    spec,
)


class TestSpec(unittest.TestCase):
    def test_fields(self):
        self.assertEqual(fields(TASK), ("instruction",))
        self.assertEqual(fields(KNOWLEDGE), ("name", "context", "text"))
        self.assertEqual(fields(SKILL), ("name", "context", "text"))

    def test_id_prefix(self):
        self.assertEqual(id_prefix(TASK), "t")
        self.assertEqual(id_prefix(KNOWLEDGE), "know")
        self.assertEqual(id_prefix(SKILL), "skill")

    def test_unknown_tag(self):
        with self.assertRaises(ValueError):
            spec("nope")


class TestParseIndex(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_index("t0"), 0)
        self.assertEqual(parse_index("know7"), 7)
        self.assertEqual(parse_index("skill42"), 42)

    def test_non_conforming(self):
        self.assertIsNone(parse_index("know"))       # no digits
        self.assertIsNone(parse_index("foo12bar"))   # suffix after digits
        self.assertIsNone(parse_index(""))           # empty
        self.assertIsNone(parse_index("12"))         # no prefix


class TestNextId(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(next_id(TASK, []), "t0")
        self.assertEqual(next_id(KNOWLEDGE, []), "know0")
        self.assertEqual(next_id(SKILL, []), "skill0")

    def test_monotonic(self):
        tuples = [
            ("task", "t1", "x"),
            ("knowledge", "know0", "", "", ""),
            ("knowledge", "know3", "", "", ""),
            ("skill", "skill1", "", "", ""),
        ]
        self.assertEqual(next_id(TASK, tuples), "t2")
        self.assertEqual(next_id(KNOWLEDGE, tuples), "know4")
        self.assertEqual(next_id(SKILL, tuples), "skill2")

    def test_ignores_non_conforming_ids(self):
        tuples = [("knowledge", "handwritten", "", "", "")]
        self.assertEqual(next_id(KNOWLEDGE, tuples), "know0")


class TestTemplates(unittest.TestCase):
    def test_all_template_arity(self):
        # a template longer than a tuple never matches — so arity matters
        self.assertEqual(all_template(TASK), ("task", None, None))
        self.assertEqual(all_template(KNOWLEDGE),
                         ("knowledge", None, None, None, None))

    def test_search_template(self):
        self.assertEqual(search_template(TASK, instruction="x"),
                         ("task", None, "x"))
        self.assertEqual(search_template(SKILL, name="summarize"),
                         ("skill", None, "summarize", None, None))


if __name__ == "__main__":
    unittest.main()
