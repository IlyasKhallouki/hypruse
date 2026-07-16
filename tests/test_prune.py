from hypruse import server


def test_prune_keeps_newest_20(tmp_path):
    for i in range(25):
        (tmp_path / f"shot-{1000 + i}.png").write_bytes(b"x")
    (tmp_path / "unrelated.txt").write_text("keep me")

    server._prune_shots(tmp_path)

    remaining = sorted(p.name for p in tmp_path.glob("shot-*.png"))
    assert len(remaining) == 20
    assert remaining[0] == "shot-1005.png"  # five oldest gone
    assert (tmp_path / "unrelated.txt").exists()


def test_prune_noop_under_limit(tmp_path):
    for i in range(3):
        (tmp_path / f"shot-{i}.png").write_bytes(b"x")
    server._prune_shots(tmp_path)
    assert len(list(tmp_path.glob("shot-*.png"))) == 3
