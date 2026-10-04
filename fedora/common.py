"""Shared configuration and pinned-source selection; no Nix runtime required."""

import base64
import copy
import hashlib
import json
import re
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(argv, **kwargs):
    print("+ " + " ".join(map(str, argv)), flush=True)
    return subprocess.run(list(map(str, argv)), check=True, **kwargs)


def version_tuple(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"Unsupported release version: {value}")
    return tuple(map(int, value.split(".")))


def lock_data():
    lock = json.loads((ROOT / "fedora/sources.lock.json").read_text())
    actual = json.loads((ROOT / "flake.lock").read_text())["nodes"]["nixpkgs"][
        "locked"
    ]["rev"]
    if actual != lock["nixpkgs"]:
        raise ValueError(
            "flake.lock changed: refresh and review fedora/sources.lock.json first"
        )
    for name, expected in lock["provenance"].items():
        if hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != expected:
            raise ValueError(
                f"Source selection changed in {name}: review the Fedora lock first"
            )
    return lock


def select_qemu(base, floor, versions):
    """Mirror lib/select-qemu-base.nix, including the unpatched-series case."""
    ceiling = max(versions, key=version_tuple)
    if version_tuple(ceiling) < version_tuple(floor):
        raise ValueError("Patch ceiling is below the repository's QEMU minimum")
    if version_tuple(base) < version_tuple(floor):
        return floor
    if version_tuple(base) > version_tuple(ceiling):
        return ceiling
    if base.rsplit(".", 1)[0] in {v.rsplit(".", 1)[0] for v in versions}:
        return base
    return ceiling


def plan(vendor):
    lock = lock_data()
    family = {"amd": "AMD", "intel": "Intel"}[vendor]
    patches = ROOT / "vendor/autovirt/patches"
    versions = [
        p.stem.removeprefix(f"{family}-v")
        for p in (patches / "QEMU").glob(f"{family}-v*.patch")
    ]
    package = (ROOT / "qemu/package.nix").read_text()
    floor = re.search(r'minimumVersion = "([\d.]+)";', package).group(1)
    version = select_qemu(lock["nixpkgsQemuVersion"], floor, versions)
    sha = lock["qemu"][version]["sha256"]
    sri = "sha256-" + base64.b64encode(bytes.fromhex(sha)).decode()
    if f'"{version}" = "{sri}";' not in package:
        raise ValueError("QEMU archive checksum does not match qemu/package.nix")
    patch_version = max(
        (v for v in versions if v.rsplit(".", 1)[0] == version.rsplit(".", 1)[0]),
        key=version_tuple,
    )
    edk_patch = max(
        (patches / "EDK2").glob(f"{family}-edk2-stable*.patch"), key=lambda p: p.name
    )
    return {
        "cpuVendor": vendor,
        "nixpkgs": lock["nixpkgs"],
        "qemu": {
            "version": version,
            "sha256": sha,
            "url": f"https://download.qemu.org/qemu-{version}.tar.xz",
            "patch": f"vendor/autovirt/patches/QEMU/{family}-v{patch_version}.patch",
        },
        "edk2": {**lock["edk2"], "patch": str(edk_patch.relative_to(ROOT))},
        "kernel": {
            **lock["kernel"],
            "url": f"https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-{lock['kernel']['version']}.tar.xz",
        },
    }


def merge_known(base, changes, path="config"):
    result = copy.deepcopy(base)
    for key, value in changes.items():
        if key not in base:
            raise ValueError(f"Unknown option {path}.{key}")
        if isinstance(base[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"Expected an object at {path}.{key}")  # noqa: TRY004 -- invalid configuration
            result[key] = merge_known(base[key], value, f"{path}.{key}")
        elif base[key] is not None and type(value) is not type(base[key]):
            raise ValueError(f"Wrong type for {path}.{key}")
        else:
            result[key] = value
    return result


def qemu_defaults():
    """Read only literal default arguments, never evaluate arbitrary Nix."""
    head = (ROOT / "qemu/package.nix").read_text().split("}:\n", 1)[0]
    return {
        key: json.loads(value)
        for key, value in re.findall(r'(?m)^  (\w+) \? ("[^"\n]*"|\d+),', head)
    }


def load_config(path):
    defaults = json.loads((ROOT / "fedora/config.example.json").read_text())
    defaults["qemu"] = qemu_defaults()
    for feature in (
        "vapic",
        "spinlocks",
        "frequencies",
        "vpindex",
        "synic",
        "stimer",
        "reset",
        "ipi",
        "tlbflush",
        "reenlightenment",
        "runtime",
    ):
        defaults["vm"]["hypervFeatures"][feature] = False
    config = merge_known(defaults, json.loads(Path(path).read_text()))
    if config["cpuVendor"] not in ("amd", "intel"):
        raise ValueError("cpuVendor must be amd or intel")
    if config["cpuVendor"] == "intel" and any(config["kernel"].values()):
        raise ValueError(
            "The repository's kernel patches are AMD/SVM-only; disable all kernel options for Intel"
        )
    vm = config["vm"]
    if vm["hypervMode"] not in ("hidden", "enlightened"):
        raise ValueError("hypervMode must be hidden or enlightened")
    if config["kernel"]["cpuidPassthrough"] and vm["hypervMode"] != "hidden":
        raise ValueError("CPUID passthrough requires hidden Hyper-V mode")
    if not 1 <= len(vm["hypervVendorId"]) <= 12:
        raise ValueError("hypervVendorId must contain 1–12 characters")
    if vm["macPrefix"] and not re.fullmatch(
        r"(?:[0-9A-Fa-f]{2}:){2}[0-9A-Fa-f]{2}", vm["macPrefix"]
    ):
        raise ValueError(
            "macPrefix must be three colon-separated hexadecimal bytes, or empty to preserve MACs"
        )
    if vm["pciMmio64Mb"] < 0:
        raise ValueError("pciMmio64Mb must not be negative")
    for name, value in config["qemu"].items():
        # These values enter existing shell/sed/C templates in several quoting
        # contexts. Keep a deliberately small alphabet instead of claiming one
        # escaping rule works for all three languages.
        if isinstance(value, str) and not re.fullmatch(r"[A-Za-z0-9 ._()+:/-]*", value):
            raise ValueError(
                f"qemu.{name} contains unsupported shell/sed/C metacharacters"
            )
        if type(value) is int and value <= 0:
            raise ValueError(f"qemu.{name} must be positive")
    q = config["qemu"]
    for key, length in (
        ("acpiOemId", 6),
        ("acpiOemTableId", 8),
        ("edidManufacturer", 3),
    ):
        if len(q[key]) != length:
            raise ValueError(f"qemu.{key} must contain exactly {length} characters")
    for key, length in (
        ("diskModel", 40),
        ("opticalModel", 40),
        ("diskSerial", 20),
        ("scsiVendor", 8),
        ("scsiTargetProduct", 16),
    ):
        if not 1 <= len(q[key]) <= length:
            raise ValueError(f"qemu.{key} must contain 1–{length} characters")
    if not re.fullmatch(r"0x[0-9a-fA-F]{1,4}", q["edidProductCode"]):
        raise ValueError("edidProductCode must be a 16-bit hexadecimal value")
    if not 1 <= q["edidWeek"] <= 54 or not 1990 <= q["edidYear"] <= 2245:
        raise ValueError("EDID week/year are outside their encoded ranges")
    smbios = vm["smbios"]
    if (
        not 1 <= smbios["memory"]["count"] <= 64
        or not 1 <= smbios["memory"]["speed"] <= 65535
    ):
        raise ValueError("Invalid memory DIMM count/speed")
    for key in ("l1", "l2", "l3"):
        if not 1 <= smbios["cache"][key] <= 0x7FFFFFFF:
            raise ValueError(f"Invalid cache size {key}")
    for key in ("assocL1", "assocL2", "assocL3", "ecc"):
        if not 0 <= smbios["cache"][key] <= 255:
            raise ValueError(f"Invalid cache byte {key}")
    return config


def nix_script(relative, substitutions=None):
    """Extract the existing shell literal; reject syntax we do not implement.

    This is intentionally not a Nix interpreter. The four kernel files are
    plain indented strings, and QEMU/OVMF have a fixed substitution vocabulary.
    Unexpected interpolation must fail, never silently produce a partial port.
    """
    source = (ROOT / relative).read_text()
    opening = re.search(r"(?m)^''\n", source)
    if opening is None:
        raise ValueError(f"Missing Nix shell literal in {relative}")
    start = opening.end()
    if not source.rstrip().endswith("''"):
        raise ValueError(f"Unsupported Nix shell wrapper in {relative}")
    body = source[start : source.rfind("''")]
    if "''" in body:
        raise ValueError(f"Nix string escapes need explicit support in {relative}")
    substitutions = substitutions or {}

    def replace(match):
        expression = match.group(1)
        if expression not in substitutions:
            raise ValueError(f"Unmapped Nix expression in {relative}: {expression}")
        return str(substitutions[expression])

    return textwrap.dedent(re.sub(r"\$\{([^}]+)\}", replace, body))


def patch_script(component, config, source_plan):
    header = "#!/usr/bin/env bash\nset -euo pipefail\n"
    if component == "kernel":
        result = nix_script("kernel/prelude.nix")
        options = config["kernel"]
        if options["timing"]:
            result += nix_script("kernel/timing-patch.nix")
        if options["cpuidPassthrough"]:
            result += nix_script("kernel/cpuid-disable.nix")
        elif options["cpuidSpoof"]:
            result += nix_script("kernel/cpuid-patch.nix")
        return header + result
    patch = str(ROOT / source_plan[component]["patch"])
    # The existing upstream shell templates do not quote this interpolation.
    if not re.fullmatch(r"[A-Za-z0-9_./+-]+", patch):
        raise ValueError(
            "Build from a repository path without spaces or shell metacharacters"
        )
    substitutions = {"autovirtPatch": patch}
    if component == "edk2":
        return header + nix_script("ovmf/post-patch.nix", substitutions)
    substitutions.update(config["qemu"])
    substitutions.update(
        {
            f"toString {key}": value
            for key, value in config["qemu"].items()
            if type(value) is int
        }
    )
    substitutions['builtins.substring 0 8 (scsiVendor + "        ")'] = config["qemu"][
        "scsiVendor"
    ].ljust(8)[:8]
    substitutions.update(
        {
            "patchedOemId": "ALASKA" if config["cpuVendor"] == "amd" else "INTEL ",
            "patchedOemTableId": "A M I   "
            if config["cpuVendor"] == "amd"
            else "U Rvp   ",
        }
    )
    # Nix's substituteInPlace is the only stdenv helper used by these scripts.
    helper = """substituteInPlace() {
  python3 - "$@" <<'PY'
from pathlib import Path
import sys
path, option, before, after = sys.argv[1:]
assert option == "--replace-fail", option
p = Path(path)
data = p.read_bytes()
if before.encode() not in data:
    raise SystemExit(f"FATAL: replacement anchor absent in {p}: {before!r}")
p.write_bytes(data.replace(before.encode(), after.encode()))
PY
}
"""
    return header + helper + nix_script("qemu/post-patch.nix", substitutions)
