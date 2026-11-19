import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
MODULE='level_network'
class CliTests(unittest.TestCase):
    def invoke(self,source,output):
        return subprocess.run([sys.executable,'-m',MODULE,'--source',str(source),'--output',str(output)],capture_output=True,text=True,timeout=10)
    def test_valid_report_and_repeat(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'report.json';source=ROOT/'fixtures/input.json'
            r=self.invoke(source,p);self.assertEqual(r.returncode,0,r.stderr)
            first=p.read_bytes();json.loads(first)
            self.assertEqual(self.invoke(source,p).returncode,0);self.assertEqual(first,p.read_bytes())
    def test_invalid_inputs_preserve_report(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'report.json';source=Path(t)/'bad.json';p.write_text('old')
            for raw in ('{','{"a":1,"a":2}','{"a":NaN}','{}'):
                source.write_text(raw);r=self.invoke(source,p)
                self.assertEqual(r.returncode,2,r.stderr);self.assertEqual(p.read_text(),'old')
    def test_output_collision_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'source.json';p.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=p.read_bytes()
            self.assertEqual(self.invoke(p,p).returncode,2);self.assertEqual(before,p.read_bytes())
    def test_symlink_target_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'target';p.write_text('old');link=Path(t)/'link';link.symlink_to(p)
            self.assertEqual(self.invoke(ROOT/'fixtures/input.json',link).returncode,2);self.assertEqual(p.read_text(),'old')
    def test_missing_source_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(self.invoke(Path(t)/'missing',Path(t)/'report').returncode,2)
