from ae.cli.__main__ import main


def test_version_command(capsys):
    assert main(["version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ae ")
