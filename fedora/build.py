#!/usr/bin/env python3
"""Build the repository's pinned virtualization stack on Fedora without Nix.

All outputs stay in --work. This tool never installs packages, changes the
bootloader, binds PCI devices, or defines a VM.
"""

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from common import ROOT, load_config, patch_script, plan, run


def require(*programs):
    missing = [p for p in programs if not shutil.which(p)]
    if missing:
        raise ValueError(
            "Missing build tools: "
            + ", ".join(missing)
            + ". See fedora/README.md for Fedora dependencies."
        )


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_filter(member, destination):
    # Upstream EDK2's Unix emulator includes an absolute link to host X11
    # headers. QEMU ships it in roms/edk2 but this build does not compile that
    # emulator. Skip only that known link; retain tarfile's path/link checks.
    if (
        member.issym()
        and member.name.endswith("/roms/edk2/EmulatorPkg/Unix/Host/X11IncludeHack")
        and member.linkname == "/opt/X11/include"
    ):
        return None
    return tarfile.data_filter(member, destination)


def download(info, cache):
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / info["url"].rsplit("/", 1)[1]
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".part")
        print(f"Downloading {info['url']}", flush=True)
        with (
            urllib.request.urlopen(info["url"], timeout=60) as response,
            partial.open("wb") as output,
        ):
            shutil.copyfileobj(response, output)
        if checksum(partial) != info["sha256"]:
            raise ValueError(
                f"Checksum mismatch for {partial}; no version fallback is permitted"
            )
        partial.replace(target)
    if checksum(target) != info["sha256"]:
        raise ValueError(f"Checksum mismatch for cached {target}")
    return target


def edk_source(info, cache):
    cache.mkdir(parents=True, exist_ok=True)
    # A prefetch and a build may reach the shared cache concurrently. Git's
    # per-file locks are not sufficient to serialize recursive submodule updates.
    with (cache / ".edk2.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return edk_source_locked(info, cache)


def edk_source_locked(info, cache):
    require("git")
    target = cache / f"edk2-{info['commit']}"
    if not target.exists():
        target.mkdir(parents=True)
        run(["git", "init", target])
        run(
            [
                "git",
                "-C",
                target,
                "remote",
                "add",
                "origin",
                "https://github.com/tianocore/edk2.git",
            ]
        )
    # Fetch the immutable commit, never a moving tag or branch.
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if head.returncode or head.stdout.strip() != info["commit"]:
        run(["git", "-C", target, "fetch", "--depth=1", "origin", info["commit"]])
        run(["git", "-C", target, "checkout", "--detach", "FETCH_HEAD"])
    run(
        [
            "git",
            "-C",
            target,
            "submodule",
            "update",
            "--init",
            "--recursive",
            "--checkout",
            "--depth=1",
        ]
    )
    head = subprocess.check_output(
        ["git", "-C", str(target), "rev-parse", "HEAD"], text=True
    ).strip()
    status = subprocess.check_output(
        ["git", "-C", str(target), "status", "--porcelain", "--untracked-files=all"],
        text=True,
    )
    submodules = subprocess.check_output(
        ["git", "-C", str(target), "submodule", "status", "--recursive"], text=True
    )
    if (
        head != info["commit"]
        or status
        or any(line[:1] != " " for line in submodules.splitlines())
    ):
        raise ValueError("EDK2 cache differs from the locked commit/submodule tree")
    return target


def fingerprint(component, config, source_plan):
    return hashlib.sha256(
        (
            json.dumps(config, sort_keys=True)
            + json.dumps(source_plan, sort_keys=True)
            + patch_script(component, config, source_plan)
        ).encode()
    ).hexdigest()


def prepare(component, config, source_plan, work):
    require("bash", "patch")
    if component == "edk2":
        require("filterdiff")
    dest = work / "sources" / component
    stamp = dest / ".vfio-prepared.json"
    expected = fingerprint(component, config, source_plan)
    if dest.exists():
        if stamp.exists() and json.loads(stamp.read_text())["fingerprint"] == expected:
            return dest
        raise ValueError(
            f"{dest} is incomplete or uses a different configuration; choose a fresh --work directory"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Prepare transactionally so a failed hunk never becomes a usable tree.
    with tempfile.TemporaryDirectory(prefix=f"{component}-", dir=dest.parent) as temp:
        temp = Path(temp)
        if component == "edk2":
            pristine = edk_source(source_plan[component], work / "downloads")
            source = temp / "tree"
            shutil.copytree(
                pristine, source, ignore=shutil.ignore_patterns(".git"), symlinks=True
            )
        else:
            archive = download(source_plan[component], work / "downloads")
            with tarfile.open(archive) as tar:
                tar.extractall(temp, filter=source_filter)
            candidates = list(temp.iterdir())
            if len(candidates) != 1 or not candidates[0].is_dir():
                raise ValueError(f"Unexpected source archive layout: {archive}")
            source = candidates[0]
        run(
            ["bash", "-s"],
            input=patch_script(component, config, source_plan),
            text=True,
            cwd=source,
        )
        (source / ".vfio-prepared.json").write_text(
            json.dumps({"fingerprint": expected, "sources": source_plan}, indent=2)
            + "\n"
        )
        source.rename(dest)
    return dest


def install_file(source, dest, executable=False):
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    dest.chmod(0o755 if executable else 0o644)


def build_tools(config, stage, work):
    require("iasl")
    vendor = config["cpuVendor"]
    assets = stage / f"usr/share/vfio-stealth/{vendor}"
    temp = work / "acpi"
    temp.mkdir(parents=True, exist_ok=True)
    for name in ("spoofed-devices", "fake-battery", "sensor-probes"):
        shutil.copy2(ROOT / f"acpi/{name}.dsl", temp / f"{name}.dsl")
        run(["iasl", "-p", name, f"{name}.dsl"], cwd=temp)
        install_file(temp / f"{name}.aml", assets / f"acpi/{name}.aml")
    cache = config["vm"]["smbios"]["cache"]
    args = []
    for option, key in (
        ("cache-l1", "l1"),
        ("cache-l2", "l2"),
        ("cache-l3", "l3"),
        ("assoc-l1", "assocL1"),
        ("assoc-l2", "assocL2"),
        ("assoc-l3", "assocL3"),
        ("ecc", "ecc"),
    ):
        args += [f"--{option}", str(cache[key])]
    run(
        [
            sys.executable,
            ROOT / "smbios/generate-tables.py",
            *args,
            "--output-dir",
            assets / "smbios",
        ]
    )
    run(
        [
            sys.executable,
            ROOT / "smbios/generate-tables.py",
            "--verify",
            assets / "smbios",
        ]
    )
    for name in ("verify-host.sh", "verify-stealth.ps1", "cleanup-registry.ps1"):
        install_file(ROOT / "guest" / name, assets / "guest" / name)
    install_file(
        ROOT / "smbios/extract.sh",
        stage / f"usr/bin/smbios-extract-stealth-{vendor}",
        True,
    )


def build_qemu(config, source_plan, work, stage, jobs):
    require("gcc", "make", "ninja", "pkg-config")
    source = prepare("qemu", config, source_plan, work)
    build = work / "qemu-build"
    build.mkdir(exist_ok=True)
    prefix = f"/usr/libexec/vfio-stealth/{config['cpuVendor']}"
    run(
        [
            source / "configure",
            f"--prefix={prefix}",
            "--target-list=x86_64-softmmu",
            "--enable-kvm",
            "--enable-seccomp",
            "--enable-slirp",
            "--enable-spice",
            "--enable-libusb",
            "--enable-usb-redir",
            "--disable-download",
            "--disable-werror",
            "--disable-docs",
        ],
        cwd=build,
    )
    run(["ninja", "-j", jobs], cwd=build)
    run(["ninja", "install"], cwd=build, env={**os.environ, "DESTDIR": str(stage)})
    emulator = stage / prefix.lstrip("/") / "bin/qemu-system-x86_64"
    run([emulator, "--version"])


def build_edk2(config, source_plan, work, stage, jobs):
    require("gcc", "g++", "make", "nasm", "iasl", "virt-fw-vars")
    source = prepare("edk2", config, source_plan, work)
    run(
        ["make", "-C", "BaseTools", "-j", jobs],
        cwd=source,
        env={**os.environ, "PYTHON_COMMAND": "python3"},
    )
    # edksetup.sh is an upstream shell environment script, not a CLI executable.
    # Parameters are positional argv, never interpolated shell source.
    command = """set -eo pipefail
export WORKSPACE="$PWD" PYTHON_COMMAND=python3
source ./edksetup.sh BaseTools
build -a X64 -t GCC -b RELEASE -p OvmfPkg/OvmfPkgX64.dsc -n "$1" \
  -D NETWORK_IP6_ENABLE=TRUE -D SECURE_BOOT_ENABLE=TRUE -D SMM_REQUIRE=TRUE \
  -D FD_SIZE_4MB -D TPM_ENABLE -D TPM2_ENABLE -D TPM2_CONFIG_ENABLE
"""
    run(["bash", "-c", command, "edk2-build", jobs], cwd=source)
    fv = source / "Build/OvmfX64/RELEASE_GCC/FV"
    dest = stage / f"usr/share/edk2/vfio-stealth-{config['cpuVendor']}"
    for name in ("OVMF_CODE.fd", "OVMF_VARS.fd"):
        if not (fv / name).is_file() or (fv / name).stat().st_size == 0:
            raise ValueError(f"Missing firmware output: {fv / name}")
        install_file(fv / name, dest / name)
    # Enroll into our freshly built 4M variable store, never copy Fedora's
    # unrelated firmware or overwrite any existing VM's mutable NVRAM.
    run(
        [
            "virt-fw-vars",
            "--input",
            dest / "OVMF_VARS.fd",
            "--output",
            dest / "OVMF_VARS.ms.fd",
            "--enroll-microsoft",
            "--secure-boot",
        ]
    )


def build_kernel(config, source_plan, work, jobs, kernel_config):
    if config["cpuVendor"] != "amd" or not any(config["kernel"].values()):
        raise ValueError("Kernel build requires at least one enabled AMD kernel patch")
    if not kernel_config or not Path(kernel_config).is_file():
        raise ValueError(
            "kernel requires --kernel-config /path/to/config (e.g. the Fedora /boot/config-...)"
        )
    require("make", "gcc", "rpmbuild", "bison", "flex", "openssl", "bc", "pahole")
    source = prepare("kernel", config, source_plan, work)
    output = work / "kernel-build"
    output.mkdir(exist_ok=True)
    shutil.copy2(kernel_config, output / ".config")
    # Keep the caller's signing policy. No silent disabling of Secure Boot.
    run(["make", f"O={output}", "olddefconfig"], cwd=source)
    settings = (output / ".config").read_text()
    for name in ("KVM", "KVM_AMD"):
        if not any(f"CONFIG_{name}={value}\n" in settings for value in ("y", "m")):
            raise ValueError(f"The provided config must enable CONFIG_{name}")
    run(
        [
            "make",
            f"O={output}",
            "-j",
            jobs,
            "LOCALVERSION=-vfio-stealth",
            "binrpm-pkg",
            f"RPMOPTS=--define '_topdir {work / 'kernel-rpmbuild'}'",
        ],
        cwd=source,
    )


def check_kernel(config, source_plan, work, jobs):
    """Compile the patched KVM objects without building/installing a full kernel."""
    require("make", "gcc", "bison", "flex")
    source = prepare("kernel", config, source_plan, work)
    output = work / "kvm-check"
    run(["make", f"O={output}", "x86_64_defconfig"], cwd=source)
    run(
        [
            source / "scripts/config",
            "--file",
            output / ".config",
            "--enable",
            "KVM",
            "--module",
            "KVM_AMD",
            "--enable",
            "KVM_HYPERV",
            "--disable",
            "DEBUG_INFO_BTF",
        ],
        cwd=source,
    )
    run(["make", f"O={output}", "olddefconfig"], cwd=source)
    run(["make", f"O={output}", "-j", jobs, "modules_prepare"], cwd=source)
    run(["make", f"O={output}", "-j", jobs, "arch/x86/kvm/"], cwd=source)
    if not (output / "arch/x86/kvm/svm/svm.o").is_file():
        raise ValueError("KVM compilation produced no svm.o")


def make_rpm(config, source_plan, work):
    require("rpmbuild")
    stage = work / "stage"
    stamp = stage / ".vfio-build.json"
    if not stamp.exists():
        raise ValueError("Run build successfully before rpm")
    expected = {"config": config, "sources": source_plan}
    if json.loads(stamp.read_text()) != expected:
        raise ValueError(
            "Staged build/config mismatch; rebuild using a fresh --work directory"
        )
    top = work / "rpmbuild"
    for folder in ("SOURCES", "SPECS", "BUILD", "BUILDROOT", "RPMS", "SRPMS"):
        (top / folder).mkdir(parents=True, exist_ok=True)
    with tarfile.open(top / "SOURCES/stack.tar.gz", "w:gz") as tar:
        tar.add(stage / "usr", arcname="stack/usr")
    vendor = config["cpuVendor"]
    qversion = source_plan["qemu"]["version"]
    spec = f"""%global debug_package %{{nil}}
%global __brp_mangle_shebangs %{{nil}}
Name: vfio-stealth-{vendor}
Version: {qversion}
Release: 1%{{?dist}}
Summary: Repository-pinned QEMU, OVMF and VFIO identity tables ({vendor})
License: GPL-2.0-only AND BSD-2-Clause-Patent AND MIT
Source0: stack.tar.gz
ExclusiveArch: x86_64
Requires: python3 bash dmidecode coreutils

%description
Locally built, patched QEMU {qversion}, EDK2 {source_plan["edk2"]["version"]},
ACPI tables, SMBIOS tables and guest verification tools. Installed alongside
Fedora's virtualization packages; activate explicitly in a libvirt domain.

%prep
%setup -q -n stack

%install
mkdir -p %{{buildroot}}
cp -a usr %{{buildroot}}/

%files
/usr/libexec/vfio-stealth/{vendor}
/usr/share/edk2/vfio-stealth-{vendor}
/usr/share/vfio-stealth/{vendor}
/usr/bin/smbios-extract-stealth-{vendor}
"""
    path = top / "SPECS/vfio-stealth.spec"
    path.write_text(spec)
    run(["rpmbuild", "-bb", "--define", f"_topdir {top}", path])
    print(f"RPMs: {top / 'RPMS'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "plan",
            "fetch",
            "prepare",
            "build",
            "rpm",
            "kernel",
            "check-kernel",
            "patch-script",
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=ROOT / "fedora/config.example.json"
    )
    parser.add_argument("--work", type=Path, default=ROOT / "build/fedora")
    parser.add_argument(
        "--component", choices=("qemu", "edk2", "kernel", "all"), default="all"
    )
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 2, 8))
    parser.add_argument("--kernel-config", type=Path)
    args = parser.parse_args()
    config = load_config(args.config)
    source_plan = plan(config["cpuVendor"])
    if args.command == "plan":
        print(json.dumps(source_plan, indent=2))
        return
    if args.command == "patch-script":
        if args.component == "all":
            raise ValueError("patch-script requires --component")
        print(patch_script(args.component, config, source_plan), end="")
        return
    if args.jobs < 1:
        raise ValueError("--jobs must be positive")
    work = args.work.resolve()
    if any(c.isspace() for c in str(work)) or "'" in str(work):
        raise ValueError(
            "--work must not contain spaces or single quotes (kernel RPM make constraints)"
        )
    work.mkdir(parents=True, exist_ok=True)
    components = (
        ("qemu", "edk2", "kernel") if args.component == "all" else (args.component,)
    )
    if args.command == "fetch":
        for component in components:
            if component == "edk2":
                edk_source(source_plan[component], work / "downloads")
            else:
                download(source_plan[component], work / "downloads")
    elif args.command == "prepare":
        for component in components:
            prepare(component, config, source_plan, work)
    elif args.command == "build":
        stage = work / "stage"
        stage.mkdir(exist_ok=True)
        # A failed rebuild must not leave a previous success marker behind.
        (stage / ".vfio-build.json").unlink(missing_ok=True)
        build_tools(config, stage, work)
        build_qemu(config, source_plan, work, stage, args.jobs)
        build_edk2(config, source_plan, work, stage, args.jobs)
        manifest = {"config": config, "sources": source_plan}
        assets = stage / f"usr/share/vfio-stealth/{config['cpuVendor']}"
        (assets / "build-manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        install_file(ROOT / "LICENSE", assets / "LICENSE")
        (stage / ".vfio-build.json").write_text(json.dumps(manifest, indent=2) + "\n")
    elif args.command == "rpm":
        make_rpm(config, source_plan, work)
    elif args.command == "kernel":
        build_kernel(config, source_plan, work, args.jobs, args.kernel_config)
    elif args.command == "check-kernel":
        check_kernel(config, source_plan, work, args.jobs)


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        tarfile.TarError,
        subprocess.CalledProcessError,
    ) as error:
        sys.exit(f"ERROR: {error}")
