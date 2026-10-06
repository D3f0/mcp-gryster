from mcp_gryster.__main__ import build_parser


def test_cli_defaults_to_stdio() -> None:
    args = build_parser().parse_args([])
    assert args.transport == "stdio"


def test_cli_accepts_streamable_http_configuration() -> None:
    args = build_parser().parse_args(
        ["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "8765"]
    )
    assert (args.transport, args.host, args.port) == ("streamable-http", "127.0.0.1", 8765)
