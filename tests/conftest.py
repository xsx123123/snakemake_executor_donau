from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def sidecar_binary():
    subprocess.run(["cargo", "build", "--manifest-path", str(ROOT / "rust/Cargo.toml"), "--quiet"],
                   check=True, timeout=180)
    return ROOT / "rust/target/debug/donau-sidecar"
