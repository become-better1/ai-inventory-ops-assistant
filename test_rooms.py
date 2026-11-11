import tempfile
from pathlib import Path
import unittest
from service import Assistant
from database import connect

class RoomChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.api=Assistant(Path(self.tmp.name)/'rooms.sqlite3')
        self.r=self.api.create_room('room',name='书房')['room_id']
        self.a=self.api.record_owned('a',name='电脑')['item_id']
        self.b=self.api.record_owned('b',name='鼠标',quantity=2)['item_id']

    def tearDown(self):
        self.tmp.cleanup()

    def move_a(self):
        return self.api.move_item('move',self.a,room_id=self.r,
            expected_version=1,location_detail='书桌')

    def test_create_list_rename_retry_and_undo(self):
        original=self.api.create_room('room',name='书房')
        self.assertEqual(original['room_id'],self.r)
        renamed=self.api.rename_room('rename',self.r,name='工作室',expected_version=1)
        self.assertEqual(self.api.rooms()[0]['name'],'工作室')
        self.api.undo('undo',renamed['operation_id'])
        self.assertEqual(self.api.rooms()[0]['name'],'书房')

    def test_duplicate_name_and_invalid_room(self):
        for name in ['书房',' 书房 ']:
            with self.assertRaises(ValueError):
                self.api.create_room('duplicate',name=name)
        with self.assertRaises(ValueError):
            self.api.create_room('empty',name=' ')
        other=self.api.create_room('other',name='卧室')['room_id']
        with self.assertRaises(ValueError):
            self.api.rename_room('duplicate_rename',other,name='书房',expected_version=1)

    def test_move_query_undo(self):
        moved=self.move_a()
        self.assertEqual(len(self.api.inventory(room_id=self.r)),1)
        self.assertEqual(len(self.api.inventory(unclassified=True)),1)
        self.assertEqual(self.api.inventory(room_id=self.r)[0]['location_detail'],'书桌')
        self.api.undo('undo',moved['operation_id'])
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)
        self.assertIsNone(self.api.inventory()[0]['location_detail'])

    def test_unclassify_clears_old_detail_and_undo_restores(self):
        self.move_a()
        moved=self.api.move_item('clear',self.a,room_id=None,expected_version=2)
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)
        self.api.undo('undo',moved['operation_id'])
        self.assertEqual(self.api.inventory(room_id=self.r)[0]['location_detail'],'书桌')

    def test_invalid_destination_and_stale_item(self):
        with self.assertRaises(ValueError):
            self.api.move_item('missing',self.a,room_id='missing',expected_version=1)
        self.move_a()
        with self.assertRaises(ValueError):
            self.api.move_item('stale',self.a,room_id=None,expected_version=1)
        self.assertEqual(len(self.api.inventory(room_id=self.r)),1)

    def test_batch_confirmation_scope_retry_undo(self):
        plan=self.api.preview_move([self.b,self.a],room_id=self.r,location_detail='柜子')
        self.assertEqual(plan['group_count'],2)
        self.assertEqual(sum(i['quantity'] for i in plan['items']),3)
        with self.assertRaises(ValueError):
            self.api.move_items('batch',plan=plan)
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)
        result=self.api.move_items('batch',plan=plan,confirmed=True)
        self.assertEqual(result,self.api.move_items('batch',plan=plan,confirmed=True))
        self.assertEqual(len(self.api.inventory(room_id=self.r)),2)
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)

    def test_stale_batch_rolls_back_entire_scope(self):
        plan=self.api.preview_move([self.a,self.b],room_id=self.r)
        self.api.update_item('edit',self.b,expected_version=1,feedback='still using')
        with self.assertRaises(ValueError):
            self.api.move_items('batch',plan=plan,confirmed=True)
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)

    def test_room_rename_invalidates_preview(self):
        plan=self.api.preview_move([self.a],room_id=self.r)
        self.api.rename_room('rename',self.r,name='新书房',expected_version=1)
        with self.assertRaises(ValueError):
            self.api.move_items('batch',plan=plan,confirmed=True)

    def test_delete_clears_and_undo_restores(self):
        self.move_a()
        plan=self.api.preview_delete_room(self.r)
        with self.assertRaises(ValueError):
            self.api.delete_room('delete',plan=plan)
        result=self.api.delete_room('delete',plan=plan,confirmed=True)
        self.assertEqual(result,self.api.delete_room('delete',plan=plan,confirmed=True))
        self.assertEqual(self.api.rooms(),[])
        self.assertEqual(len(self.api.inventory(unclassified=True)),2)
        self.api.undo('undo',result['operation_id'])
        self.assertEqual(self.api.rooms()[0]['id'],self.r)
        self.assertEqual(self.api.inventory(room_id=self.r)[0]['location_detail'],'书桌')

    def test_new_arrival_invalidates_delete_preview(self):
        self.move_a()
        plan=self.api.preview_delete_room(self.r)
        self.api.move_item('bmove',self.b,room_id=self.r,expected_version=1)
        with self.assertRaises(ValueError):
            self.api.delete_room('delete',plan=plan,confirmed=True)
        self.assertEqual(len(self.api.inventory(room_id=self.r)),2)

    def test_deletion_undo_conflict_no_partial_restore(self):
        self.move_a()
        deletion=self.api.delete_room('delete',plan=self.api.preview_delete_room(self.r),confirmed=True)
        item=next(i for i in self.api.inventory() if i['id']==self.a)
        self.api.update_item('later',self.a,expected_version=item['version'],feedback='new data')
        with self.assertRaises(ValueError):
            self.api.undo('undo',deletion['operation_id'])
        self.assertEqual(self.api.rooms(),[])

    def test_cannot_undo_room_creation_with_live_item_reference(self):
        room=self.api.create_room('extra',name='客厅')
        self.api.move_item('move',self.a,room_id=room['room_id'],expected_version=1)
        with self.assertRaises(ValueError):
            self.api.undo('undo',room['operation_id'])
        self.assertEqual(len(self.api.inventory(room_id=room['room_id'])),1)

    def test_deleted_room_name_can_be_reused_and_blocks_restore_collision(self):
        deletion=self.api.delete_room('delete',plan=self.api.preview_delete_room(self.r),confirmed=True)
        replacement=self.api.create_room('replacement',name='书房')
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.api.undo('undo',deletion['operation_id'])
        self.assertEqual(self.api.rooms()[0]['id'],replacement['room_id'])

    def test_exited_item_room_reference_cleared_on_delete(self):
        self.move_a()
        self.api.return_item('return',self.a,quantity=1,expected_version=2)
        deletion=self.api.delete_room('delete',plan=self.api.preview_delete_room(self.r),confirmed=True)
        c=connect(self.api.path)
        try:
            self.assertIsNone(c.execute('SELECT room_id FROM items WHERE id=?',(self.a,)).fetchone()[0])
        finally:
            c.close()
        self.api.undo('undo',deletion['operation_id'])
        self.assertEqual(len(self.api.inventory(room_id=self.r)),0)

    def test_invalid_scope_and_filters(self):
        for ids in ([],[self.a,self.a],['missing']):
            with self.assertRaises(ValueError):
                self.api.preview_move(ids,room_id=self.r)
        with self.assertRaises(ValueError):
            self.api.inventory(room_id=self.r,unclassified=True)
        with self.assertRaises(ValueError):
            self.api.preview_delete_room(None)

if __name__=='__main__':
    unittest.main(verbosity=2)

