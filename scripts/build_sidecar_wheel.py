#!/usr/bin/env python3
"""Build a host-platform wheel with the Rust binary; no publication occurs.

Requires poetry-core. Run from the repository root with a Rust toolchain.
The resulting linux wheel targets this host's libc; it is not a manylinux wheel.
"""
import base64
import csv
import hashlib
import io
from pathlib import Path
import subprocess
import sysconfig
import tempfile
import zipfile

from poetry.core.masonry.api import build_wheel


def main():
    root = Path(__file__).resolve().parents[1]
    if Path.cwd() != root:
        raise SystemExit("Run from the repository root")
    subprocess.run(["cargo", "build", "--release", "--locked", "--manifest-path", "rust/Cargo.toml"], check=True)
    binary = root / "rust/target/release/donau-sidecar"
    output_dir = root / "dist"
    output_dir.mkdir(exist_ok=True)
    platform = sysconfig.get_platform().replace("-", "_").replace(".", "_")
    with tempfile.TemporaryDirectory() as temporary:
        pure_name = build_wheel(temporary)
        name = pure_name.replace("py3-none-any.whl", "py3-none-{}.whl".format(platform))
        with zipfile.ZipFile(Path(temporary) / pure_name) as source:
            entries = {item.filename: source.read(item) for item in source.infolist()}
        record = next(path for path in entries if path.endswith(".dist-info/RECORD"))
        wheel = next(path for path in entries if path.endswith(".dist-info/WHEEL"))
        entries[wheel] = entries[wheel].replace(b"Root-Is-Purelib: true", b"Root-Is-Purelib: false").replace(
            b"Tag: py3-none-any", "Tag: py3-none-{}".format(platform).encode())
        executable = "snakemake_executor_plugin_donau/bin/donau-sidecar"
        entries[executable] = binary.read_bytes()
        rows = []
        for path, data in entries.items():
            if path == record:
                continue
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rows.append([path, "sha256=" + digest, str(len(data))])
        rows.append([record, "", ""])
        buffer = io.StringIO(newline="")
        csv.writer(buffer).writerows(rows)
        entries[record] = buffer.getvalue().encode()
        with zipfile.ZipFile(output_dir / name, "w", zipfile.ZIP_DEFLATED) as target:
            for path, data in entries.items():
                info = zipfile.ZipInfo(path)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if path == executable else 0o100644) << 16
                target.writestr(info, data)
        print(output_dir / name)


if __name__ == "__main__":
    main()
