"""Global test fixtures — runs before any test module is imported."""

import os
import tempfile

# Redirect state.json to a temp directory so tests don't clobber
# a running server's state file.
_test_state_dir = tempfile.mkdtemp(prefix="quern-test-")
os.environ["QUERN_STATE_DIR"] = _test_state_dir
