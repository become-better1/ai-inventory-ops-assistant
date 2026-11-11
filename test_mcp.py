"""Protocol-level tests with real MCP SDK clients; only temporary databases."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from mcp import Client, StdioServerParameters
from mcp_server import create_server, DESCRIPTIONS, ToolAdapter

class MCPChecks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'mcp.sqlite3'
        self.client=Client(create_server(self.path),raise_exceptions=True)

    def _callTestMethod(self, method):
        # AnyIO client contexts must enter and exit in the SAME asyncio task.
        async def run():
            async with self.client:
                await method()
        self._callMaybeAsync(run)

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def call(self,tool_name,**request):
        result=await self.client.call_tool(tool_name,{'request':request})
        self.assertFalse(result.is_error)
        self.assertIsNotNone(result.structured_content)
        return result.structured_content

    async def buy(self):
        result=await self.call('record_purchase',request_key='buy',name='鼠标',
            quantity=2,total_amount_minor=40000,currency='CNY')
        self.assertTrue(result['ok'])
        return result['data']

    async def test_schema_discovery_and_annotations(self):
        tools=(await self.client.list_tools()).tools
        self.assertEqual({t.name for t in tools},set(DESCRIPTIONS))
        for tool in tools:
            self.assertEqual(tool.annotations.read_only_hint,DESCRIPTIONS[tool.name][1])
            self.assertIn('request',tool.input_schema['properties'])
        execute=next(t for t in tools if t.name=='move_items')
        self.assertIn('preview_id',json.dumps(execute.input_schema))
        self.assertNotIn('"plan"',json.dumps(execute.input_schema))

    async def test_purchase_query_return_undo(self):
        b=await self.buy()
        repeated=await self.call('record_purchase',request_key='buy',name='鼠标',
            quantity=2,total_amount_minor=40000,currency='CNY')
        self.assertEqual(repeated['data'],b)
        r=(await self.call('return_item',request_key='return',item_id=b['item_id'],
            quantity=1,expected_version=1,infer_amount=True))['data']
        self.assertEqual((await self.call('spending'))['data']['by_currency']['CNY']['net_minor'],20000)
        await self.call('undo',request_key='undo',operation_id=r['operation_id'])
        inventory=(await self.call('inventory'))['data']
        self.assertEqual(sum(i['quantity'] for i in inventory),2)
        self.assertEqual((await self.call('spending'))['data']['by_currency']['CNY']['net_minor'],40000)

    async def test_domain_errors_and_duplicate_candidates(self):
        await self.call('record_owned',request_key='owned',name='电脑')
        duplicate=await self.call('record_owned',request_key='repeat',name='电脑')
        self.assertFalse(duplicate['ok'])
        self.assertEqual(duplicate['error']['code'],'POSSIBLE_DUPLICATE')
        self.assertEqual(len(duplicate['error']['candidates']),1)
        stale=await self.call('update_item',request_key='stale',item_id='missing',
            expected_version=1,changes={'feedback':'hello'})
        self.assertFalse(stale['ok'])

    async def test_strict_types_and_extra_fields(self):
        for request in (
            dict(request_key='bad',name='鼠标',quantity=True),
            dict(request_key='bad',name='鼠标',quantity='2'),
            dict(request_key='bad',name='鼠标',quantity=1,db='C:/other.sqlite3')):
            result=await self.client.call_tool('record_purchase',{'request':request})
            self.assertTrue(result.is_error)
        self.assertEqual((await self.call('inventory'))['data'],[])

    async def test_changes_cannot_override_key_or_identity(self):
        b=await self.buy()
        result=await self.call('update_item',request_key='edit',item_id=b['item_id'],
            expected_version=1,changes={'request_key':'override'})
        self.assertFalse(result['ok'])
        self.assertEqual((await self.call('inventory'))['data'][0]['version'],1)

    async def test_preview_confirmation_and_saved_scope(self):
        b=await self.buy()
        room=(await self.call('create_room',request_key='room',name='书房'))['data']
        preview=(await self.call('preview_move',item_ids=[b['item_id']],room_id=room['room_id']))['data']
        denied=await self.call('move_items',request_key='move',preview_id=preview['preview_id'])
        self.assertFalse(denied['ok'])
        moved=await self.call('move_items',request_key='move',preview_id=preview['preview_id'],confirmed=True)
        self.assertTrue(moved['ok'])
        retry=await self.call('move_items',request_key='move',preview_id=preview['preview_id'],confirmed=True)
        self.assertEqual(retry,moved)
        items=(await self.call('inventory',room_id=room['room_id']))['data']
        self.assertEqual(len(items),1)
        wrong=await self.call('delete_room',request_key='wrong',preview_id=preview['preview_id'],confirmed=True)
        self.assertFalse(wrong['ok'])

    async def test_stale_preview_and_unknown_token(self):
        b=await self.buy()
        preview=(await self.call('preview_move',item_ids=[b['item_id']],room_id=None))['data']
        await self.call('update_item',request_key='edit',item_id=b['item_id'],
            expected_version=1,changes={'feedback':'new'})
        stale=await self.call('move_items',request_key='move',preview_id=preview['preview_id'],confirmed=True)
        self.assertFalse(stale['ok'])
        unknown=await self.call('move_items',request_key='unknown',preview_id='invented',confirmed=True)
        self.assertFalse(unknown['ok'])

    async def test_rule_crud_via_mcp(self):
        r=(await self.call('create_rule',request_key='rule',content='超过五百元先考虑三天。'))['data']
        await self.call('update_rule',request_key='disable',rule_id=r['rule_id'],
            expected_version=1,changes={'is_active':False})
        self.assertEqual((await self.call('rules',active_only=True))['data'],[])
        deleted=await self.call('delete_rule',request_key='delete',rule_id=r['rule_id'],expected_version=2)
        self.assertTrue(deleted['ok'])
        self.assertEqual((await self.call('rules'))['data'],[])

    async def test_room_rename_and_delete_via_preview(self):
        r=(await self.call('create_room',request_key='room',name='书房'))['data']
        await self.call('rename_room',request_key='rename',room_id=r['room_id'],name='工作室',expected_version=1)
        self.assertEqual((await self.call('rooms'))['data'][0]['name'],'工作室')
        plan=(await self.call('preview_delete_room',room_id=r['room_id']))['data']
        deleted=await self.call('delete_room',request_key='delete',preview_id=plan['preview_id'],confirmed=True)
        self.assertTrue(deleted['ok'])
        self.assertEqual((await self.call('rooms'))['data'],[])

    async def test_quantity_preview_and_return_correction(self):
        b=await self.buy()
        plan=(await self.call('preview_quantity_correction',name='鼠标',target_total=3))['data']
        corrected=await self.call('correct_quantity',request_key='quantity',preview_id=plan['preview_id'])
        self.assertTrue(corrected['ok'])
        self.assertEqual(sum(i['quantity'] for i in (await self.call('inventory'))['data']),3)
        r=(await self.call('return_item',request_key='return',item_id=b['item_id'],
            quantity=1,expected_version=1,infer_amount=True))['data']
        events=(await self.call('return_events',purchase_id=b['purchase_id']))['data']
        edited=await self.call('correct_return_amount',request_key='amount',event_id=r['event_id'],
            expected_version=events[0]['version'],amount_minor=18000)
        self.assertTrue(edited['ok'])
        self.assertEqual((await self.call('spending'))['data']['by_currency']['CNY']['net_minor'],22000)

    async def test_order_conflict_visible(self):
        args=dict(name='鼠标',quantity=1,total_amount_minor=20000,currency='CNY',
            merchant='shop',external_order_id='1',external_line_id='1',source_kind='order_image')
        await self.call('record_purchase',request_key='buy',**args)
        args['quantity']=2
        error=await self.call('record_purchase',request_key='conflict',**args)
        self.assertEqual(error['error']['code'],'ORDER_CONFLICT')

    async def test_purchase_amount_correction_protocol(self):
        p=(await self.call('record_purchase',request_key='umbrella',name='雨伞',quantity=1))['data']
        rows=(await self.call('purchase_records',purchase_id=p['purchase_id']))['data']
        result=await self.call('correct_purchase_amount',request_key='price',purchase_id=p['purchase_id'],
            expected_version=rows[0]['version'],total_amount_minor=2300,currency='CNY')
        self.assertTrue(result['ok'])
        self.assertEqual(sum(i['quantity'] for i in (await self.call('inventory'))['data']),1)
        self.assertEqual((await self.call('spending'))['data']['by_currency']['CNY']['net_minor'],2300)

class ProcessChecks(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_process(self):
        asyncio.get_running_loop().set_debug(False)
        with tempfile.TemporaryDirectory() as folder:
            params=StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).with_name('mcp_server.py')),'--db',str(Path(folder)/'stdio.sqlite3')],
                cwd=Path(__file__).parent)
            async with asyncio.timeout(30):
                async with Client(params,mode='legacy') as c:
                    tools=(await c.list_tools()).tools
                    self.assertEqual(len(tools),len(DESCRIPTIONS))
                    result=await c.call_tool('record_owned',{'request':{'request_key':'own','name':'旧电脑'}})
                    self.assertFalse(result.is_error)
                    self.assertTrue(result.structured_content['ok'])
                    inventory=await c.call_tool('inventory',{'request':{}})
                    self.assertEqual(len(inventory.structured_content['data']),1)
                    from maintenance import snapshot,restore
                    backup=snapshot(Path(folder)/'stdio.sqlite3',Path(folder)/'backups')
                    with self.assertRaises(ValueError):
                        restore(backup['backup'],Path(folder)/'stdio.sqlite3',confirmed=True)

class PreviewExpiryChecks(unittest.TestCase):
    def test_expired_token_and_capacity(self):
        with tempfile.TemporaryDirectory() as folder:
            adapter=ToolAdapter(Path(folder)/'expiry.sqlite3')
            adapter.previews.ttl=0
            token=adapter.previews.put('move_items',{})
            with self.assertRaises(ValueError):
                adapter.previews.get(token,'move_items')
            adapter.previews.capacity=1
            first=adapter.previews.put('move_items',{})
            adapter.previews.put('move_items',{})
            with self.assertRaises(ValueError):
                adapter.previews.get(first,'move_items')

if __name__=='__main__':
    unittest.main(verbosity=2)

