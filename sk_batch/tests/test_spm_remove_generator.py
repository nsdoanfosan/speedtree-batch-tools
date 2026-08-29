import gzip
import sys
from pathlib import Path


SK_BATCH = Path(__file__).resolve().parents[1]
if str(SK_BATCH) not in sys.path:
    sys.path.insert(0, str(SK_BATCH))

from spm_remove_generator import remove_generator_text


def test_remove_generator_preserves_unrelated_xml():
    guid = "cap-guid"
    text = """<SpeedTree>
\t<Generators>
\t\t<Generator Type=\"Branch\"><GUID>keep</GUID></Generator>
\t\t<Generator Type=\"Cap\"><Name>Cap 22</Name><GUID>cap-guid</GUID></Generator>
\t</Generators>
\t<Links>
\t\t<Link><SourceGUID>branch</SourceGUID><TargetGUID>cap-guid</TargetGUID></Link>
\t\t<Link><SourceGUID>branch</SourceGUID><TargetGUID>keep</TargetGUID></Link>
\t</Links>
\t<Nodes>
\t\t<Node Type=\"Cap\"><GeneratorGUID>cap-guid</GeneratorGUID></Node>
\t\t<Node Type=\"Branch\"><GeneratorGUID>keep</GeneratorGUID></Node>
\t</Nodes>
</SpeedTree>"""

    updated, counts = remove_generator_text(text, guid)

    assert counts == {"generators": 1, "links": 1, "nodes": 1}
    assert guid not in updated
    assert '<Generator Type="Branch"><GUID>keep</GUID></Generator>' in updated
    assert "<TargetGUID>keep</TargetGUID>" in updated
    assert '<Node Type="Branch"><GeneratorGUID>keep</GeneratorGUID></Node>' in updated


def test_spm_module_import_does_not_mutate_gzip_fixture(tmp_path):
    fixture = tmp_path / "tree.spm"
    fixture.write_bytes(gzip.compress(b"<SpeedTree />", mtime=0))
    before = fixture.read_bytes()

    assert fixture.read_bytes() == before
