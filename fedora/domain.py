#!/usr/bin/env python3
"""Convert exported x86 Q35 libvirt XML; write a new file, never define a VM."""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from common import load_config

QEMU = "http://libvirt.org/schemas/domain/qemu/1.0"
META = "urn:vfio-stealth:fedora:1"
ET.register_namespace("qemu", QEMU)
ET.register_namespace("vfio", META)


def child(parent, tag, **attrs):
    node = parent.find(tag)
    if node is None:
        node = ET.SubElement(parent, tag)
    node.attrib.update(attrs)
    return node


def replace(parent, tag, **attrs):
    for node in parent.findall(tag):
        parent.remove(node)
    return ET.SubElement(parent, tag, attrs)


def smbios_escape(value):
    value = str(value)
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ValueError("SMBIOS values must be printable ASCII")
    return value.replace(",", ",,")


def convert(xml, config, nvram, kernel_config=None, host_threads=None):
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("DTD/entity declarations are not supported in domain XML")
    root = ET.fromstring(xml)
    if root.tag != "domain" or root.get("type") != "kvm":
        raise ValueError("Expected a KVM <domain>")
    if root.find(f"metadata/{{{META}}}configuration") is not None:
        raise ValueError(
            "This XML was already converted; start from the original domain export"
        )
    os_node = root.find("os")
    os_type = None if os_node is None else os_node.find("type")
    if (
        os_type is None
        or os_type.get("arch") != "x86_64"
        or not re.fullmatch(r"(?:pc-)?q35(?:-[\d.]+)?", os_type.get("machine", ""))
    ):
        raise ValueError(
            "Expected an existing x86_64 Q35 VM; machine/chipset conversion is not automatic"
        )
    if not root.findtext("uuid"):
        raise ValueError("Export a defined domain with a UUID before converting")
    if not nvram.startswith("/var/lib/libvirt/qemu/nvram/") or not re.fullmatch(
        r"/[A-Za-z0-9_./-]+", nvram
    ):
        raise ValueError(
            "Use a new absolute NVRAM path under /var/lib/libvirt/qemu/nvram/"
        )
    if ".." in Path(nvram).parts:
        raise ValueError("NVRAM path cannot contain '..'")
    if nvram == os_node.findtext("nvram"):
        raise ValueError(
            "Use a new NVRAM path; the original VM's variable store must remain available"
        )
    vm = config["vm"]
    smbios = vm["smbios"]
    for key in ("manufacturer", "product", "baseBoardSerial"):
        if smbios[key] == "CHANGE-ME":
            raise ValueError(
                f"Set vm.smbios.{key} to your intended identity before converting a VM"
            )
    if config["kernel"]["cpuidPassthrough"]:
        vcpus = root.find("vcpu")
        if (
            host_threads is None
            or vcpus is None
            or int(vcpus.text) != host_threads
            or int(vcpus.get("current", vcpus.text)) != host_threads
        ):
            raise ValueError(
                "CPUID passthrough requires --host-threads and every host thread present as a guest vCPU"
            )
    features_requested = vm["hypervFeatures"]
    dependent = [
        k
        for k, value in features_requested.items()
        if value and k not in ("vendor_id", "relaxed")
    ]
    if (
        vm["hypervMode"] == "enlightened"
        and dependent
        and (
            kernel_config is None
            or not re.search(r"^CONFIG_KVM_HYPERV=y$", kernel_config, re.MULTILINE)
        )
    ):
        raise ValueError(
            "Requested Hyper-V features require a --kernel-config containing CONFIG_KVM_HYPERV=y: "
            + ", ".join(dependent)
        )
    commandline = child(root, f"{{{QEMU}}}commandline")
    old_args = [node.get("value", "") for node in commandline.findall(f"{{{QEMU}}}arg")]
    conflicts = {
        "-cpu",
        "-smbios",
        "-acpitable",
        "-fw_cfg",
        "-global",
        "-overcommit",
        "-bios",
        "-pflash",
    }.intersection(old_args)
    if conflicts:
        raise ValueError(
            "Existing raw QEMU settings need manual reconciliation first: "
            + ", ".join(sorted(conflicts))
        )
    vendor = config["cpuVendor"]
    prefix = f"/usr/libexec/vfio-stealth/{vendor}"
    firmware = f"/usr/share/edk2/vfio-stealth-{vendor}"
    assets = f"/usr/share/vfio-stealth/{vendor}"
    devices = child(root, "devices")
    child(devices, "emulator").text = f"{prefix}/bin/qemu-system-x86_64"
    os_node.attrib.pop("firmware", None)
    for node in os_node.findall("firmware"):
        os_node.remove(node)
    replace(
        os_node, "loader", readonly="yes", secure="yes", type="pflash", format="raw"
    ).text = f"{firmware}/OVMF_CODE.fd"
    replace(
        os_node,
        "nvram",
        template=f"{firmware}/OVMF_VARS.ms.fd",
        templateFormat="raw",
        format="raw",
    ).text = nvram
    replace(os_node, "smbios", mode="sysinfo")

    cpu = child(root, "cpu")
    cpu.attrib = {"mode": "host-passthrough", "check": "none", "migratable": "off"}
    # Preserve topology/NUMA and unrelated CPU tuning, replacing only our keys.
    for node in list(cpu):
        if node.tag in ("model", "vendor") or (
            node.tag == "feature"
            and node.get("name") in ("hypervisor", "topoext", "invtsc")
        ):
            cpu.remove(node)
    for name in ("topoext", "invtsc"):
        ET.SubElement(cpu, "feature", policy="optional", name=name)
    if vm["hypervMode"] == "hidden":
        ET.SubElement(cpu, "feature", policy="disable", name="hypervisor")

    features = child(root, "features")
    child(features, "acpi")
    child(features, "apic")
    child(features, "smm", state="on")
    child(features, "vmport", state="off")
    kvm = child(features, "kvm")
    for name in ("hidden", "hint-dedicated", "poll-control"):
        child(kvm, name, state="on")
    for node in features.findall("hyperv"):
        features.remove(node)
    if vm["hypervMode"] == "enlightened" and any(features_requested.values()):
        hyperv = ET.SubElement(features, "hyperv", mode="custom")
        for name, enabled in features_requested.items():
            if not enabled:
                continue
            node = ET.SubElement(hyperv, name, state="on")
            if name == "vendor_id":
                node.set("value", vm["hypervVendorId"])
            elif name == "spinlocks":
                node.set("retries", "8191")
            elif name == "stimer":
                ET.SubElement(node, "direct", state="on")
    clock = child(root, "clock", offset="localtime")
    for key in ("adjustment", "basis", "timezone", "start"):
        clock.attrib.pop(key, None)
    timers = {
        "rtc": {"tickpolicy": "catchup"},
        "pit": {"tickpolicy": "delay"},
        "hpet": {"present": "yes"},
        "kvmclock": {"present": "no"},
        "hypervclock": {
            "present": "yes"
            if dependent and vm["hypervMode"] == "enlightened"
            else "no"
        },
        "tsc": {"present": "yes", "mode": "native"},
    }
    for node in list(clock):
        if node.tag == "timer" and node.get("name") in timers:
            clock.remove(node)
    for name, attrs in timers.items():
        ET.SubElement(clock, "timer", name=name, **attrs)

    for node in root.findall("sysinfo"):
        if node.get("type") == "smbios":
            root.remove(node)
    sysinfo = ET.SubElement(root, "sysinfo", type="smbios")
    groups = {
        "bios": {
            "vendor": smbios["biosVendor"],
            "version": smbios["biosVersion"],
            "date": smbios["biosDate"],
            "release": smbios["biosRelease"],
        },
        "system": {
            "manufacturer": smbios["manufacturer"],
            "product": smbios["product"],
            "serial": smbios["serial"],
            "uuid": root.findtext("uuid"),
            "family": "To be filled by O.E.M.",
        },
        "baseBoard": {
            "manufacturer": smbios["manufacturer"],
            "product": smbios["product"],
            "version": smbios["baseBoardVersion"],
            "serial": smbios["baseBoardSerial"],
            "asset": smbios["baseBoardAsset"],
            "location": smbios["baseBoardLocation"],
        },
    }
    for name, values in groups.items():
        group = ET.SubElement(sysinfo, name)
        for key, value in values.items():
            ET.SubElement(group, "entry", name=key).text = value

    if vm["stripVirtio"]:
        for node in list(devices):
            if (
                node.tag in ("memballoon", "rng")
                and node.get("model", "").startswith("virtio")
                or node.tag == "input"
                and node.get("bus") == "virtio"
            ):
                devices.remove(node)
        if devices.find("memballoon") is None:
            ET.SubElement(devices, "memballoon", model="none")
    for node in devices.findall("panic"):
        devices.remove(node)
    if vm["macPrefix"]:
        if int(vm["macPrefix"][:2], 16) & 1:
            raise ValueError("macPrefix must identify unicast addresses")
        for interface in devices.findall("interface"):
            mac = interface.find("mac")
            if mac is not None:
                address = mac.get("address", "")
                if not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", address):
                    raise ValueError("Invalid existing MAC address")
                mac.set("address", vm["macPrefix"].lower() + address[8:])

    def arg(option, value):
        ET.SubElement(commandline, f"{{{QEMU}}}arg", value=option)
        ET.SubElement(commandline, f"{{{QEMU}}}arg", value=value)

    esc = smbios_escape
    arg(
        "-smbios",
        f"type=3,manufacturer={esc(smbios['manufacturer'])},version=1.0,serial=Default string,asset=Default string,sku=Default string",
    )
    for name in (
        "type7-l1",
        "type7-l2",
        "type7-l3",
        "type26",
        "type27",
        "type28",
        "type29",
    ):
        arg("-smbios", f"file={assets}/smbios/{name}.bin")
    arg("-smbios", "type=8,internal_reference=USB 3.2 Gen 2,port_type=9")
    arg(
        "-smbios",
        "type=9,slot_designation=PCIEX16_1,slot_type=0xa5,current_usage=3,slot_length=4",
    )
    for key, name in (
        ("spoofedDevices", "spoofed-devices"),
        ("fakeBattery", "fake-battery"),
        ("sensorProbes", "sensor-probes"),
    ):
        if vm["acpiSsdt"][key]:
            arg("-acpitable", f"file={assets}/acpi/{name}.aml")
    arg("-overcommit", "cpu-pm=on")
    if vm["pciMmio64Mb"]:
        arg("-fw_cfg", f"opt/ovmf/X-PciMmio64Mb,string={vm['pciMmio64Mb']}")
    for state in ("s3", "s4"):
        arg("-global", f"ICH9-LPC.disable_{state}=0")
    # Properties not in libvirt's CPU feature map. Use QOM globals so there is
    # exactly one -cpu (libvirt's), retaining all generated Hyper-V features.
    arg(
        "-global",
        "host-x86_64-cpu.kvm-pv-enforce-cpuid="
        + ("on" if vm["kvmPvEnforceCpuid"] else "off"),
    )
    arg(
        "-global", "host-x86_64-cpu.aperfmperf=" + ("on" if vm["aperfMperf"] else "off")
    )
    identity = vm["cpuIdentity"]
    if identity:
        if (
            not isinstance(identity, dict)
            or set(identity) - {"modelId", "manufacturer", "maxSpeed", "currentSpeed"}
            or not isinstance(identity.get("modelId"), str)
        ):
            raise ValueError(
                "cpuIdentity requires modelId and optional manufacturer/maxSpeed/currentSpeed"
            )
        arg("-global", f"host-x86_64-cpu.model-id={esc(identity['modelId'])}")
        manufacturer = identity.get(
            "manufacturer",
            "Advanced Micro Devices, Inc." if vendor == "amd" else "Intel Corporation",
        )
        value = f"type=4,sock_pfx={esc(smbios['socketPrefix'])},manufacturer={esc(manufacturer)},version={esc(identity['modelId'])}"
        for key, option in (
            ("maxSpeed", "max-speed"),
            ("currentSpeed", "current-speed"),
        ):
            if key in identity:
                if type(identity[key]) is not int or not 1 <= identity[key] <= 65535:
                    raise ValueError(f"Invalid cpuIdentity.{key}")
                value += f",{option}={identity[key]}"
        arg("-smbios", value)
    memory = smbios["memory"]
    for i in range(memory["count"]):
        arg(
            "-smbios",
            f"type=17,loc_pfx=DIMM_,bank=BANK {i},speed={memory['speed']},part={esc(memory['partNumber'])},serial=0000000{i},manufacturer={esc(memory['manufacturer'])}",
        )
    if smbios["oemStrings"]:
        arg(
            "-smbios",
            "type=11"
            + "".join(f",value={esc(value)}" for value in smbios["oemStrings"]),
        )
    for device in smbios["onboardDevices"]:
        if (
            set(device) != {"designation", "kind", "instance"}
            or device["kind"]
            not in (
                "other",
                "unknown",
                "video",
                "scsi",
                "ethernet",
                "tokenring",
                "sound",
                "pata",
                "sata",
                "sas",
            )
            or type(device["instance"]) is not int
            or not 0 <= device["instance"] <= 255
        ):
            raise ValueError("Invalid SMBIOS onboard device")
        arg(
            "-smbios",
            f"type=41,designation={esc(device['designation'])},kind={device['kind']},instance={device['instance']}",
        )
    metadata = child(root, "metadata")
    ET.SubElement(
        metadata,
        f"{{{META}}}configuration",
        cpuVendor=vendor,
        hypervMode=vm["hypervMode"],
    )
    ET.indent(root)
    return ET.tostring(root, encoding="unicode") + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--nvram", required=True)
    parser.add_argument("--kernel-config", type=Path)
    parser.add_argument("--host-threads", type=int)
    args = parser.parse_args()
    if Path(args.nvram).exists():
        raise ValueError("The new NVRAM path already exists; choose an unused path")
    config = load_config(args.config)
    output = convert(
        args.input.read_text(),
        config,
        args.nvram,
        args.kernel_config.read_text() if args.kernel_config else None,
        args.host_threads,
    )
    with args.output.open("x") as stream:
        stream.write(output)
    print(f"Wrote {args.output}. Validate with: virt-xml-validate {args.output} domain")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, ET.ParseError) as error:
        sys.exit(f"ERROR: {error}")
