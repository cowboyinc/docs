import ast
import unittest

from gen_sdk_reference import sig


class SignatureTests(unittest.TestCase):
    def test_positional_and_keyword_only_markers(self):
        fn = ast.parse("def example(a: int, /, b=2, *, c: str = 'x'): pass").body[0]
        self.assertEqual(sig(fn), "example(a: int, /, b = 2, *, c: str = 'x')")

    def test_positional_only_defaults_and_varargs(self):
        fn = ast.parse("def example(a=1, /, *args, **kwargs): pass").body[0]
        self.assertEqual(sig(fn), "example(a = 1, /, *args, **kwargs)")


if __name__ == "__main__":
    unittest.main()
