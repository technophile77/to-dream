import random
import tempfile
import unittest
from pathlib import Path

from helpers import new_seed

from to_dream import redact

# Fake secrets are assembled at runtime so no key-shaped literal exists in this file.
ALNUM = "aB3dE5gH7jK9mN1pQ3sT5vW7yZ9bC1dE3fG5"
POSITIVE = {
    "private-key-header": "-----BEGIN RSA " + "PRIVATE KEY-----",
    "aws-access-key-id": "id = " + "AKIA" + "IOSFODNN7EXAMPLQ",
    "github-token": "t " + "ghp_" + ALNUM,
    "slack-token": "x " + "xox" + "b-" + "1234567890-abcdefghij",
    "anthropic-api-key": "k " + "sk-" + "ant-api03-" + ALNUM,
    "openai-api-key": "k " + "sk-" + "proj-" + ALNUM,
    "stripe-live-key": "k " + "sk_" + "live_" + ALNUM[:24],
    "google-api-key": "k " + "AI" + "za" + ALNUM[:35],
    "jwt": "h " + "eyJ" + "hbGciOiJIUzI1" + "." + "eyJ" + "zdWIiOiIxMjM0" + ".sig",
    "generic-secret-assignment": "password: " + "hunter2" + "hunter2",
}
NEGATIVE = [
    "the password is stored in SSM",
    "password: stored in SSM",
    "token: see 1Password",
    "API key lives in the vault, ask the admin",
    "password = <your-password>",
    "password: ${DB_PASSWORD}",
    "api_key=" + "x" * 12,
    "secret: ********",
    "password: changeme123",
    "token: example-token-value",
    "token = os.environ['GITHUB_TOKEN']",
    "token = get_token(user)",
    "client_secret: CLIENT_SECRET_ENV",
    "max_tokens: 4096",
    "sk-learn is a library with a long name-ish thing",
    "reset the passwords of all users",
    "run `git config user.name` first",
]


class ScanLineTests(unittest.TestCase):
    def test_each_rule_fires_by_name(self):
        for rule, line in POSITIVE.items():
            with self.subTest(rule=rule):
                self.assertIn(rule, redact.scan_line(line))

    def test_assignment_variants(self):
        value = "hunter2" + "hunter2"
        for line in [f"DB_PASSWORD={value}", f'"password": "{value}"', f"**password**: {value}",
                     f"client_secret = '{value}'", f"api-key: {value}", f"AWS_SECRET_ACCESS_KEY={value}"]:
            with self.subTest(line=line):
                self.assertEqual(redact.scan_line(line), ["generic-secret-assignment"])

    def test_negatives_are_clean(self):
        for line in NEGATIVE:
            with self.subTest(line=line):
                self.assertEqual(redact.scan_line(line), [])

    def test_allow_marker_exempts_line(self):
        self.assertEqual(redact.scan_line(POSITIVE["github-token"] + "  secret-scan: allow"), [])

    def test_random_prose_with_trigger_words_is_clean(self):
        seed = new_seed()
        rng = random.Random(seed)
        words = ["the", "password", "token", "secret", "api", "key", "is", "in", "SSM", "vault", "stored", "rotate",
                 "never", "commit"]
        for _ in range(500):
            line = " ".join(rng.choice(words) for _ in range(rng.randint(3, 12)))
            with self.subTest(seed=seed, line=line):
                self.assertEqual(redact.scan_line(line), [])

    def test_random_secret_values_are_caught(self):
        seed = new_seed()
        rng = random.Random(seed)
        chars = "abcdefghijklmnopqrstuvwyz0123456789"  # no "x": runs of x / "example" mark placeholders
        for _ in range(300):
            value = "".join(rng.choice(chars) for _ in range(rng.randint(10, 30))) + "7"
            key = rng.choice(["password", "passwd", "secret", "api_key", "token", "client_secret"])
            sep = rng.choice([": ", "=", " = ", ":"])
            with self.subTest(seed=seed, key=key):
                self.assertEqual(redact.scan_line(key + sep + value), ["generic-secret-assignment"])


class RedactTests(unittest.TestCase):
    def test_each_token_rule_is_replaced_by_name(self):
        for rule, line in POSITIVE.items():
            if rule == "generic-secret-assignment":
                continue
            with self.subTest(rule=rule):
                out = redact.redact("before " + line + " after")
                self.assertIn(f"[REDACTED:{rule}]", out)
                self.assertTrue(out.startswith("before ") and out.endswith(" after"))
                self.assertNotIn(ALNUM, out)

    def test_assignment_keeps_key_and_hides_value(self):
        value = "hunter2" + "hunter2"
        self.assertEqual(
            redact.redact(f'"password": "{value}", other'),
            '"password": "[REDACTED:generic-secret-assignment]", other',
        )
        self.assertEqual(redact.redact(f"DB_PASSWORD={value}"), "DB_PASSWORD=[REDACTED:generic-secret-assignment]")

    def test_negatives_unchanged(self):
        for line in NEGATIVE:
            with self.subTest(line=line):
                self.assertEqual(redact.redact(line), line)

    def test_redacted_output_scans_clean_and_is_idempotent(self):
        for line in POSITIVE.values():
            with self.subTest(line=line):
                once = redact.redact(line)
                self.assertEqual(redact.scan_line(once), [])
                self.assertEqual(redact.redact(once), once)

    def test_allow_marker_does_not_stop_redaction(self):
        out = redact.redact(POSITIVE["github-token"] + "  secret-scan: allow")
        self.assertIn("[REDACTED:github-token]", out)

    def test_random_secret_values_never_survive(self):
        seed = new_seed()
        rng = random.Random(seed)
        chars = "abcdefghijklmnopqrstuvwyz0123456789"  # no "x": runs of x / "example" mark placeholders
        for _ in range(300):
            value = "".join(rng.choice(chars) for _ in range(rng.randint(10, 30))) + "7"
            key = rng.choice(["password", "passwd", "secret", "api_key", "token"])
            out = redact.redact(f"run {key}={value} now")
            with self.subTest(seed=seed, key=key):
                self.assertNotIn(value, out)
                self.assertTrue(out.startswith(f"run {key}=") and out.endswith(" now"))


class DiffParsingTests(unittest.TestCase):
    DIFF = (
        "diff --git a/p/memory/a.md b/p/memory/a.md\n"
        "new file mode 100644\n--- /dev/null\n+++ b/p/memory/a.md\n"
        "@@ -0,0 +1,2 @@\n+first\n+second\n"
        "diff --git a/p/memory/b.md b/p/memory/b.md\n--- a/p/memory/b.md\n+++ b/p/memory/b.md\n"
        "@@ -3,2 +3,0 @@\n-gone\n-gone\n"
        "@@ -10,0 +8,1 @@\n+++ looks like a header\n"
        "diff --git a/p/memory/c.md b/p/memory/c.md\ndeleted file mode 100644\n--- a/p/memory/c.md\n+++ /dev/null\n"
        "@@ -1 +0,0 @@\n-x\n"
    )

    def test_iter_added_lines(self):
        self.assertEqual(
            list(redact.iter_added_lines(self.DIFF)),
            [("p/memory/a.md", 1, "first"), ("p/memory/a.md", 2, "second"),
             ("p/memory/b.md", 8, "++ looks like a header")],
        )

    def test_scan_diff_reports_new_file_line_numbers(self):
        secret = POSITIVE["github-token"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1,0 +5,2 @@\n+ok\n+" + secret + "\n"
        self.assertEqual(redact.scan_diff(diff), [redact.Finding("f", 6, "github-token")])

    def test_removed_secret_lines_are_not_findings(self):
        secret = POSITIVE["github-token"]
        diff = "diff --git a/f b/f\n--- a/f\n+++ b/f\n@@ -1 +1 @@\n-" + secret + "\n+clean\n"
        self.assertEqual(redact.scan_diff(diff), [])

    def test_scan_text_numbers_lines_from_one(self):
        text = "fine\n" + POSITIVE["jwt"] + "\nalso fine\n" + POSITIVE["generic-secret-assignment"]
        self.assertEqual(
            redact.scan_text("p/memory/a.md", text),
            [redact.Finding("p/memory/a.md", 2, "jwt"), redact.Finding("p/memory/a.md", 4, "generic-secret-assignment")],
        )

    def test_scan_files_reads_relative_paths_and_skips_missing_ones(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "p" / "memory").mkdir(parents=True)
            (root / "p" / "memory" / "a.md").write_text("fine\n" + POSITIVE["github-token"] + "\n", newline="\n")
            (root / "p" / "memory" / "b.md").write_text("clean\n", newline="\n")
            found = redact.scan_files(root, ["p/memory/a.md", "p/memory/b.md", "p/memory/gone.md"])
        self.assertEqual(found, [redact.Finding("p/memory/a.md", 2, "github-token")])

    def test_formatting_has_no_values(self):
        finding = redact.Finding("p/memory/a.md", 3, "jwt")
        self.assertEqual(redact.format_finding(finding), "p/memory/a.md:3 jwt")
        self.assertIn("p/memory/a.md:3 jwt", redact.format_findings([finding]))


if __name__ == "__main__":
    unittest.main()
