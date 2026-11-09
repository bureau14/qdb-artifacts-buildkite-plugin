import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import artifacts

ROOT = Path(__file__).resolve().parents[1]
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")
BASH = str(GIT_BASH) if GIT_BASH.is_file() else shutil.which("bash")


def run_hook(source, env):
    with tempfile.TemporaryDirectory() as directory:
        script = Path(directory) / "post-command"
        with script.open("w", newline="\n") as stream:
            stream.write(source)
        return subprocess.run([BASH, script.as_posix()], env=env, capture_output=True, check=False)


class EmptyUploadTests(unittest.TestCase):
    def test_empty_upload_needs_no_store_and_creates_no_annotation(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "artifact.zip"
            artifact.write_bytes(b"artifact")
            for patterns, excludes in [
                ([str(Path(directory) / "missing-*")], []),
                ([str(artifact)], ["*"]),
            ]:
                for allow_empty in [False, True]:
                    with contextlib.ExitStack() as stack, self.subTest(allow_empty=allow_empty):
                        store = stack.enter_context(patch.object(artifacts, "aws_clients"))
                        annotation = stack.enter_context(
                            patch.object(artifacts, "_create_buildkite_annotation")
                        )
                        manifest = stack.enter_context(patch.object(artifacts, "_upload_manifest"))
                        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
                        kwargs = {
                            "base_dir": directory,
                            "exclude_patterns": excludes,
                            "annotate": True,
                            "allow_empty": allow_empty,
                        }
                        if allow_empty:
                            self.assertIs(
                                artifacts.upload("p", "b", "ref", "v", patterns, **kwargs),
                                False,
                            )
                        else:
                            with self.assertRaises(SystemExit) as error:
                                artifacts.upload("p", "b", "ref", "v", patterns, **kwargs)
                            self.assertEqual(error.exception.code, 1)
                        store.assert_not_called()
                        annotation.assert_not_called()
                        manifest.assert_not_called()

    def test_nonempty_upload_still_uploads_and_annotates(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
            artifact = Path(directory) / "artifact.zip"
            artifact.write_bytes(b"artifact")
            stack.enter_context(patch.object(artifacts, "aws_clients", return_value=(None, None)))
            stack.enter_context(patch.object(artifacts, "load_store_config"))
            stack.enter_context(patch.object(artifacts, "resolve_object_auth"))
            stack.enter_context(patch.object(artifacts, "scope", return_value=("bucket", "prefix")))
            client = stack.enter_context(patch.object(artifacts, "_s3_client"))
            manifest = stack.enter_context(patch.object(artifacts, "_upload_manifest"))
            annotation = stack.enter_context(
                patch.object(artifacts, "_create_buildkite_annotation")
            )
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertIs(
                artifacts.upload(
                    "p", "b", "ref", "v", [str(artifact)], base_dir=directory, allow_empty=True
                ),
                True,
            )
            client.return_value.upload_file.assert_called_once()
            manifest.assert_called_once()
            annotation.assert_called_once()

    def test_cli_reports_empty_only_when_requested_by_hook(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env.update(
                BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_VARIANT="v",
                BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_GIT_REF="refs/heads/test",
                BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_FILES=str(Path(directory) / "missing-*"),
                BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_ALLOW_EMPTY="true",
                BUILDKITE_PIPELINE_SLUG="project",
                BUILDKITE_BUILD_ID="build",
            )
            for args, expected in [([], 0), (["--report-empty"], 3)]:
                with self.subTest(args=args):
                    result = subprocess.run(
                        [sys.executable, str(ROOT / "lib/artifacts.py"), "upload"] + args,
                        env=env,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected, result.stderr.decode())

    @unittest.skipUnless(BASH, "Bash is required to exercise the post-command hook")
    def test_hook_promotes_only_after_completed_upload(self):
        source = (ROOT / "hooks/post-command").read_text()
        # Stub only the shared runner; execute the real hook's control flow.
        source = source.replace(
            'source "${DIR}/../lib/shared.bash"',
            """plugin_read_config() { echo "${2:-}"; }
run_artifacts_py() {
  echo "CALL $*"
  if [[ "$1" == upload ]]; then return "$TEST_UPLOAD_STATUS"; fi
}""",
        )
        for status, command_status, upload, expected, promote in [
            (3, 0, True, 0, False),
            (0, 0, True, 0, True),
            (1, 0, True, 1, False),
            (2, 0, True, 2, False),
            (0, 1, True, 0, False),
            (0, 0, False, 0, True),
        ]:
            with self.subTest(status=status, command_status=command_status, upload=upload):
                env = dict(os.environ)
                env.update(
                    TEST_UPLOAD_STATUS=str(status),
                    BUILDKITE_COMMAND_EXIT_STATUS=str(command_status),
                    BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_FILES="*.zip" if upload else "",
                    BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_FILES_0="",
                    BUILDKITE_PLUGIN_QDB_ARTIFACTS_PROMOTE_VARIANT="v",
                    BUILDKITE_PLUGIN_QDB_ARTIFACTS_DEBUG="false",
                )
                result = run_hook(source, env)
                self.assertEqual(result.returncode, expected, result.stderr.decode())
                self.assertEqual("CALL promote" in result.stdout.decode(), promote)
                if upload:
                    self.assertIn("CALL upload --report-empty", result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
