"""Room and location operations; mixed into the local structured Assistant API."""
from contextlib import closing
from database import connect
from service_types import positive, text

class RoomOperations:
    def rooms(self):
        with closing(connect(self.path)) as c:
            return [dict(r) for r in c.execute(
                'SELECT * FROM rooms WHERE is_void=0 ORDER BY name,id')]

    def create_room(self, request_key, *, name, room_type=None):
        text(name,'Room name')
        if room_type is not None:
            text(room_type,'Room type')
        name = name.strip()
        def run(w):
            self._room_name_available(w.c,name)
            return dict(room_id=w.insert('rooms',dict(name=name,room_type=room_type)))
        return self._write(request_key,'create_room',dict(name=name,room_type=room_type),run)

    @staticmethod
    def _room_name_available(c,name,exclude=None):
        for row in c.execute('SELECT id,name FROM rooms WHERE is_void=0'):
            if row['id']!=exclude and row['name'].strip().casefold()==name.strip().casefold():
                raise ValueError('Room name already exists')

    @staticmethod
    def _active_room(c,room_id):
        if room_id is None:
            return None
        text(room_id,'Room ID')
        row=c.execute('SELECT * FROM rooms WHERE id=? AND is_void=0',(room_id,)).fetchone()
        if not row:
            raise ValueError('Room missing or removed')
        return dict(row)

    def rename_room(self,request_key,room_id,*,name,expected_version):
        text(name,'Room name')
        positive(expected_version,'Version')
        name=name.strip()
        def run(w):
            room=self._active_room(w.c,room_id)
            if room is None or room['version']!=expected_version:
                raise ValueError('Room changed; refresh first')
            self._room_name_available(w.c,name,room_id)
            w.update('rooms',room_id,dict(name=name))
            return dict(room_id=room_id)
        return self._write(request_key,'rename_room',
            dict(room_id=room_id,name=name,expected_version=expected_version),run)

    def move_item(self,request_key,item_id,*,room_id,expected_version,location_detail=None):
        text(item_id,'Item ID')
        positive(expected_version,'Version')
        if location_detail is not None and not isinstance(location_detail,str):
            raise ValueError('Location detail must be text or null')
        def run(w):
            self._active_room(w.c,room_id)
            item=w.get('items',item_id)
            if item['is_void'] or item['ownership_status']!='held' or item['version']!=expected_version:
                raise ValueError('Item changed or no longer held')
            w.update('items',item_id,dict(room_id=room_id,location_detail=location_detail))
            w.event(item_id,'location_changed')
            return dict(item_id=item_id,room_id=room_id,quantity=item['quantity'])
        return self._write(request_key,'move_item',dict(item_id=item_id,room_id=room_id,
            expected_version=expected_version,location_detail=location_detail),run)

    @staticmethod
    def _ids(item_ids):
        if not isinstance(item_ids,list) or not item_ids:
            raise ValueError('Provide a nonempty list of item IDs')
        for item_id in item_ids:
            text(item_id,'Item ID')
        if len(set(item_ids))!=len(item_ids):
            raise ValueError('Duplicate item ID')
        return sorted(item_ids)

    @staticmethod
    def _plan(c,*,kind,item_ids=None,room_id=None,location_detail=None):
        room=RoomOperations._active_room(c,room_id)
        if kind=='delete_room':
            if room is None:
                raise ValueError('Select a room to delete')
            rows=c.execute('SELECT * FROM items WHERE room_id=? AND is_void=0 ORDER BY id',(room_id,)).fetchall()
        else:
            rows=[]
            for item_id in item_ids:
                row=c.execute("SELECT * FROM items WHERE id=? AND is_void=0 AND ownership_status='held'",(item_id,)).fetchone()
                if row is None:
                    raise ValueError('Item missing or no longer held')
                rows.append(row)
        # Snapshot includes room identity/version and every affected ID, count and version.
        return dict(kind=kind,room=None if room is None else
            dict(id=room['id'],name=room['name'],version=room['version']),
            items=[dict(id=r['id'],name=r['name'],quantity=r['quantity'],unit=r['unit'],
                version=r['version'],room_id=r['room_id'],location_detail=r['location_detail'])
                for r in rows],
            group_count=len(rows),location_detail=location_detail)

    def preview_move(self,item_ids,*,room_id,location_detail=None):
        ids=self._ids(item_ids)
        if location_detail is not None and not isinstance(location_detail,str):
            raise ValueError('Location detail must be text or null')
        with closing(connect(self.path)) as c:
            c.execute('BEGIN')
            return self._plan(c,kind='move_items',item_ids=ids,room_id=room_id,
                              location_detail=location_detail)

    def move_items(self,request_key,*,plan,confirmed=False):
        # The caller must show the original preview and obtain confirmation.
        # This local flag is not an authentication mechanism.
        if confirmed is not True or not isinstance(plan,dict) or plan.get('kind')!='move_items':
            raise ValueError('Confirm a move preview first')
        try:
            ids=self._ids([r['id'] for r in plan['items']])
            room_id=None if plan['room'] is None else plan['room']['id']
            detail=plan['location_detail']
        except (KeyError,TypeError):
            raise ValueError('Invalid move preview') from None
        if detail is not None and not isinstance(detail,str):
            raise ValueError('Invalid location detail')
        def run(w):
            current=self._plan(w.c,kind='move_items',item_ids=ids,
                room_id=room_id,location_detail=detail)
            if current!=plan:
                raise ValueError('Preview stale; review and confirm a new preview')
            for item_id in ids:
                w.update('items',item_id,dict(room_id=room_id,location_detail=detail))
                w.event(item_id,'location_changed')
            return dict(item_ids=ids,room_id=room_id,group_count=len(ids))
        return self._write(request_key,'move_items',dict(plan=plan,confirmed=True),run)

    def preview_delete_room(self,room_id):
        with closing(connect(self.path)) as c:
            c.execute('BEGIN')
            return self._plan(c,kind='delete_room',room_id=room_id)

    def delete_room(self,request_key,*,plan,confirmed=False):
        if confirmed is not True or not isinstance(plan,dict) or plan.get('kind')!='delete_room':
            raise ValueError('Confirm a deletion preview first')
        try:
            room_id=plan['room']['id']
        except (KeyError,TypeError):
            raise ValueError('Invalid deletion preview') from None
        def run(w):
            current=self._plan(w.c,kind='delete_room',room_id=room_id)
            if current!=plan:
                raise ValueError('Room or affected items changed; review a new preview')
            for item in current['items']:
                # Historical exited items are also detached, so no live row references a void room.
                w.update('items',item['id'],dict(room_id=None,location_detail=None))
                w.event(item['id'],'location_changed')
            w.update('rooms',room_id,dict(is_void=1))
            return dict(room_id=room_id,group_count=current['group_count'])
        return self._write(request_key,'delete_room',dict(plan=plan,confirmed=True),run)

