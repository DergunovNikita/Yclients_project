from pathlib import Path


def test_root_and_web_proxy_trees_are_byte_identical():
    # AGENTS.md: a route present in only one of these trees 404s in deploy. A directory
    # walk (rather than a hardcoded filename list) also catches a file added to one side only.
    root = Path(__file__).resolve().parents[1]
    root_api = root / 'api'
    web_api = root / 'web' / 'api'

    def tracked_files(directory: Path) -> set[Path]:
        return {
            path.relative_to(directory)
            for path in directory.rglob('*')
            if path.is_file() and path.name != '.DS_Store'
        }

    root_files = tracked_files(root_api)
    web_files = tracked_files(web_api)
    assert root_files == web_files, (
        f'api/ and web/api/ file sets differ: {root_files.symmetric_difference(web_files)}'
    )

    for relative_path in sorted(root_files):
        assert (root_api / relative_path).read_bytes() == (web_api / relative_path).read_bytes(), (
            f'{relative_path} differs between api/ and web/api/'
        )
