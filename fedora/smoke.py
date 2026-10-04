#!/usr/bin/env python3
"""Boot the built QEMU/OVMF pair under TCG; no KVM or host installation needed."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from common import ROOT


def smoke(work, vendor):
    stage = work / "stage"
    prefix = stage / f"usr/libexec/vfio-stealth/{vendor}"
    firmware = stage / f"usr/share/edk2/vfio-stealth-{vendor}"
    shell = work / "sources/edk2/Build/OvmfX64/RELEASE_GCC/X64/Shell.efi"
    if not shell.is_file():
        raise ValueError("Build OVMF before running the firmware smoke test")
    with tempfile.TemporaryDirectory(prefix="uefi-smoke-", dir=work) as temp:
        temp = Path(temp)
        boot = temp / "esp/EFI/BOOT"
        boot.mkdir(parents=True)
        shutil.copy2(shell, boot / "BOOTX64.EFI")
        (temp / "esp/startup.nsh").write_text(
            "echo -off\necho VFIO_FIRMWARE_BOOT_OK\nreset -s\n"
        )
        # The unsigned test shell uses a disposable unenrolled store. Never
        # alter the production store or any installed VM's mutable NVRAM.
        shutil.copy2(firmware / "OVMF_VARS.fd", temp / "VARS.fd")
        command = [
            str(prefix / "bin/qemu-system-x86_64"),
            "-L",
            str(prefix / "share/qemu"),
            "-machine",
            "q35,accel=tcg,smm=on",
            "-global",
            "driver=cfi.pflash01,property=secure,value=on",
            "-cpu",
            "max",
            "-m",
            "512",
            "-nodefaults",
            "-display",
            "none",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-no-reboot",
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={firmware / 'OVMF_CODE.fd'}",
            "-drive",
            f"if=pflash,format=raw,unit=1,file={temp / 'VARS.fd'}",
            "-drive",
            f"if=none,id=esp,format=raw,file=fat:rw:{temp / 'esp'}",
            "-device",
            "qemu-xhci,id=xhci",
            "-device",
            "usb-storage,drive=esp,bootindex=1",
        ]
        print("Booting the patched QEMU/OVMF pair under TCG...", flush=True)
        with (work / "firmware-smoke.log").open("w") as output:
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    timeout=180,
                )
            except subprocess.TimeoutExpired as error:
                raise ValueError(
                    f"Firmware smoke test timed out; see {work / 'firmware-smoke.log'}"
                ) from error
        log = (work / "firmware-smoke.log").read_text(errors="replace")
        if result.returncode or "VFIO_FIRMWARE_BOOT_OK" not in log:
            raise ValueError(
                f"Firmware did not complete the EFI shell test; see {work / 'firmware-smoke.log'}"
            )
        print("PASS: EFI shell booted and completed startup.nsh")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, default=ROOT / "build/fedora")
    parser.add_argument("--vendor", choices=("amd", "intel"), default="amd")
    args = parser.parse_args()
    smoke(args.work.resolve(), args.vendor)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(f"ERROR: {error}")
