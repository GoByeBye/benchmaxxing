import copy
import unittest

from solution import merge_intervals


class PublicExamples(unittest.TestCase):
    def test_overlap(self):
        self.assertEqual(merge_intervals([[1, 3], [2, 6], [8, 10]]), [[1, 6], [8, 10]])

    def test_touch_and_sort(self):
        values = [[4, 5], [1, 2], [2, 3]]
        original = copy.deepcopy(values)
        self.assertEqual(merge_intervals(values), [[1, 3], [4, 5]])
        self.assertEqual(values, original)


if __name__ == "__main__":
    unittest.main()
