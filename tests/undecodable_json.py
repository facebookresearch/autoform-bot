"""JSON that ``json.loads`` refuses, for tests of readers that must refuse it cleanly."""

#: Nesting past the decoder's recursion limit (RecursionError) and an integer past the digit limit (ValueError).
#: From Python 3.14 the decoder goes as deep as the stack allows, past 100,000 levels in macOS's 64 MiB maximum.
UNDECODABLE_JSON = {
    "nested": '{"schema": ' + "[" * 1_000_000 + "]" * 1_000_000 + "}",
    "a-number-too-long": '{"schema": 1' + "0" * 5000 + "}",
}
