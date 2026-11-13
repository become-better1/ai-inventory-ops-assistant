"""Export live MCP schemas using a temporary database."""
import asyncio
import json
import tempfile
from pathlib import Path
from mcp import Client
from mcp_server import create_server

async def main():
    root = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as folder:
        async with Client(create_server(Path(folder) / 'schema.sqlite3'), raise_exceptions=True) as client:
            tools = (await client.list_tools()).tools
        payload = dict(design_version='2.0', sdk='mcp==2.3.0', transport='stdio',
                       tools=[t.model_dump(mode='json', by_alias=True, exclude_none=True) for t in tools])
        (root / 'mcp-tools.schema.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Exported {len(tools)} tool schemas using temporary data.')

if __name__ == '__main__':
    asyncio.run(main())
