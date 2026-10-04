"""Offline contracts for source selection, shell rendering and domain migration."""

import copy
import json
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from common import ROOT, load_config, patch_script, plan, select_qemu
from domain import QEMU, convert

FIXTURE = """<domain type='kvm'>
<name>win11</name><uuid>8b85c86e-d29a-425e-8fe3-16879a3e8e21</uuid>
<memory unit='KiB'>8388608</memory><vcpu>4</vcpu>
<os firmware='efi'><type arch='x86_64' machine='pc-q35-10.2'>hvm</type>
<loader readonly='yes' type='pflash'>/old/CODE.fd</loader>
<nvram>/var/lib/libvirt/qemu/nvram/win11_VARS.fd</nvram></os>
<features><acpi/><apic/></features>
<cpu mode='host-model'><topology sockets='1' cores='2' threads='2'/></cpu>
<devices><emulator>/usr/bin/qemu-system-x86_64</emulator>
<disk type='file' device='disk'><driver name='qemu' type='qcow2'/>
<source file='/var/lib/libvirt/images/win11.qcow2'/><target dev='sda' bus='sata'/></disk>
<interface type='network'><mac address='52:54:00:11:22:33'/><source network='default'/><model type='e1000e'/></interface>
<hostdev mode='subsystem' type='pci' managed='yes'><source><address domain='0x0000' bus='0x01' slot='0x00' function='0x0'/></source></hostdev>
<memballoon model='virtio'/><rng model='virtio'><backend model='random'>/dev/urandom</backend></rng>
<panic model='isa'/></devices></domain>"""
NVRAM = "/var/lib/libvirt/qemu/nvram/win11_stealth_VARS.fd"


class PortTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(ROOT / "fedora/config.example.json")
        self.config["vm"]["smbios"].update(
            manufacturer="Example, Inc.",
            product="Example Board",
            baseBoardSerial="EX123",
        )

    def test_repository_versions(self):
        self.assertEqual(plan("amd")["qemu"]["version"], "11.1.0")
        self.assertEqual(plan("intel")["qemu"]["version"], "11.0.3")
        self.assertEqual(plan("amd")["edk2"]["version"], "202608")
        self.assertEqual(plan("amd")["kernel"]["version"], "7.2.8")

    def test_version_selection_boundaries(self):
        versions = ["11.0.3", "11.1.0"]
        self.assertEqual(select_qemu("10.2.2", "11.0.3", versions), "11.0.3")
        self.assertEqual(select_qemu("11.1.1", "11.0.3", versions), "11.1.0")
        self.assertEqual(select_qemu("11.0.4", "11.0.3", versions), "11.0.4")
        self.assertEqual(
            select_qemu("11.2.0", "11.0.3", ["11.0.3", "11.3.0"]), "11.3.0"
        )

    def test_all_shell_scripts_parse(self):
        for vendor in ("amd", "intel"):
            config = copy.deepcopy(self.config)
            config["cpuVendor"] = vendor
            for component in ("qemu", "edk2", "kernel"):
                with self.subTest(vendor=vendor, component=component):
                    script = patch_script(component, config, plan(vendor))
                    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
                    self.assertNotIn("${autovirtPatch}", script)
                    self.assertNotIn("${toString", script)
                    if component == "kernel":
                        self.assertIn("exactly_one()", script)
                        self.assertIn("static int handle_rdtsc_interception", script)

    def test_unknown_options_and_shell_metacharacters_fail(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.json"
            for config in (
                {"qemu": {"diskSerial": "$(touch /tmp/unsafe)"}},
                {"vm": {"hyprevMode": "hidden"}},
                {"cpuVendor": "intel"},
                {"kernel": {"cpuidPassthrough": True}},
            ):
                with self.subTest(config=config):
                    path.write_text(json.dumps(config))
                    with self.assertRaises(ValueError):
                        load_config(path)

    def test_domain_preserves_storage_uuid_gpu_topology(self):
        before = ET.fromstring(FIXTURE)
        after = ET.fromstring(convert(FIXTURE, self.config, NVRAM))
        for selector in (
            "uuid",
            "name",
            "devices/disk",
            "devices/hostdev",
            "cpu/topology",
        ):
            self.assertEqual(
                ET.canonicalize(
                    ET.tostring(before.find(selector), encoding="unicode"),
                    strip_text=True,
                ),
                ET.canonicalize(
                    ET.tostring(after.find(selector), encoding="unicode"),
                    strip_text=True,
                ),
            )
        self.assertEqual(
            after.find("devices/interface/mac").get("address"), "d8:bb:c1:11:22:33"
        )
        self.assertEqual(after.find("devices/memballoon").get("model"), "none")
        self.assertIsNone(after.find("devices/panic"))
        self.assertIsNone(after.find("devices/rng"))
        values = [
            e.get("value")
            for e in after.findall(f"{{{QEMU}}}commandline/{{{QEMU}}}arg")
        ]
        self.assertNotIn("-cpu", values)
        self.assertTrue(any("manufacturer=Example,, Inc." in value for value in values))
        self.assertEqual(after.find("os/nvram").text, NVRAM)

    def test_domain_schema(self):
        try:
            from lxml import etree
        except ImportError:
            self.skipTest("python3-lxml not available")
        schema_path = Path("/usr/share/libvirt/schemas/domain.rng")
        if not schema_path.exists():
            self.skipTest("libvirt domain schema not installed")
        schema = etree.RelaxNG(etree.parse(str(schema_path)))
        xml = convert(FIXTURE, self.config, NVRAM)
        schema.assertValid(etree.fromstring(xml.encode()))

    def test_hyperv_capability_gate_and_hidden_mode(self):
        self.config["vm"]["hypervFeatures"]["vapic"] = True
        with self.assertRaisesRegex(ValueError, "CONFIG_KVM_HYPERV"):
            convert(FIXTURE, self.config, NVRAM)
        visible = ET.fromstring(
            convert(FIXTURE, self.config, NVRAM, "CONFIG_KVM_HYPERV=y\n")
        )
        self.assertEqual(
            visible.find("clock/timer[@name='hypervclock']").get("present"), "yes"
        )
        self.config["vm"]["hypervMode"] = "hidden"
        hidden = ET.fromstring(convert(FIXTURE, self.config, NVRAM))
        self.assertIsNone(hidden.find("features/hyperv"))
        self.assertEqual(
            hidden.find("clock/timer[@name='hypervclock']").get("present"), "no"
        )

    def test_reject_destructive_or_conflicting_inputs(self):
        output = convert(FIXTURE, self.config, NVRAM)
        cases = [
            output,
            FIXTURE.replace("pc-q35-10.2", "pc-i440fx-10.2"),
            FIXTURE.replace(
                "</domain>",
                f'<qemu:commandline xmlns:qemu="{QEMU}"><qemu:arg value="-cpu"/><qemu:arg value="host"/></qemu:commandline></domain>',
            ),
        ]
        for xml in cases:
            with self.assertRaises(ValueError):
                convert(xml, self.config, NVRAM)
        with self.assertRaises(ValueError):
            convert(FIXTURE, self.config, "/var/lib/libvirt/qemu/nvram/win11_VARS.fd")

    def test_cpuid_passthrough_requires_full_topology(self):
        self.config["kernel"]["cpuidPassthrough"] = True
        self.config["vm"]["hypervMode"] = "hidden"
        with self.assertRaises(ValueError):
            convert(FIXTURE, self.config, NVRAM, host_threads=16)
        convert(FIXTURE, self.config, NVRAM, host_threads=4)


if __name__ == "__main__":
    unittest.main()
