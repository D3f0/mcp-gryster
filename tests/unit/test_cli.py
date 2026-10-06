from mcp_gryster.__main__ import build_parser


def test_cli_defaults_to_stdio() -> None:
    args = build_parser().parse_args([])
    assert args.transport == "stdio"
