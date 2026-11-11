-- Initial schema. Apply through database.py so migration checksum is recorded.
CREATE TABLE rooms (id TEXT PRIMARY KEY NOT NULL,
name TEXT NOT NULL CHECK(length(trim(name))>0),
room_type TEXT, notes TEXT, 
created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))) STRICT;

CREATE TABLE purchase_records (id TEXT PRIMARY KEY NOT NULL,
product_name TEXT NOT NULL CHECK(length(trim(product_name))>0),
category TEXT,
quantity INTEGER NOT NULL CHECK(quantity>0),
unit TEXT NOT NULL CHECK(length(trim(unit))>0),
total_amount_minor INTEGER CHECK(total_amount_minor>=0),
currency TEXT CHECK(currency IS NULL OR (length(currency)=3 AND currency=upper(currency))),
purchase_date TEXT,
date_precision TEXT NOT NULL DEFAULT 'unknown' CHECK(date_precision IN ('year','month','day','unknown')),
purchase_motive TEXT, merchant TEXT, external_order_id TEXT, external_line_id TEXT,
import_mode TEXT NOT NULL CHECK(import_mode IN ('with_inventory','history_only')),
source_kind TEXT NOT NULL CHECK(source_kind IN ('natural_language','photo','order_image','correction')), notes TEXT,

created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))
,
CHECK(total_amount_minor IS NULL OR currency IS NOT NULL),
CHECK((date_precision='unknown' AND purchase_date IS NULL) OR
(date_precision='year' AND purchase_date IS NOT NULL AND length(purchase_date)=4 AND purchase_date NOT GLOB '*[^0-9]*') OR
(date_precision='month' AND purchase_date IS NOT NULL AND purchase_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' AND substr(purchase_date,6,2) BETWEEN '01' AND '12') OR
(date_precision='day' AND purchase_date IS NOT NULL AND length(purchase_date)=10 AND coalesce(date(purchase_date,'+0 days')=purchase_date,0)))) STRICT;

CREATE TABLE items (id TEXT PRIMARY KEY NOT NULL,
name TEXT NOT NULL CHECK(length(trim(name))>0),
category TEXT, brand TEXT, model TEXT,
quantity INTEGER NOT NULL CHECK(quantity>0),
unit TEXT NOT NULL DEFAULT '件' CHECK(length(trim(unit))>0),
purchase_record_id TEXT REFERENCES purchase_records(id),
parent_item_id TEXT REFERENCES items(id) CHECK(parent_item_id IS NULL OR parent_item_id<>id),
ownership_status TEXT NOT NULL DEFAULT 'held' CHECK(ownership_status IN
('held','sold','gifted','discarded','lost','returned','cancelled','depleted','count_corrected')),
delivery_status TEXT NOT NULL DEFAULT 'unknown' CHECK(delivery_status IN ('pending','received','unknown')),
usage_status TEXT NOT NULL DEFAULT 'unknown' CHECK(usage_status IN ('in_use','not_in_use','unknown')),
condition_status TEXT NOT NULL DEFAULT 'unknown' CHECK(condition_status IN ('normal','damaged','unknown')),
disposition_intent TEXT CHECK(disposition_intent IN ('sell','gift','repair','recycle','discard')),
room_id TEXT REFERENCES rooms(id), location_detail TEXT,
common_purposes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(common_purposes_json) AND json_type(common_purposes_json)='array'),
personal_purposes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(personal_purposes_json) AND json_type(personal_purposes_json)='array'),
feedback TEXT, source_kind TEXT NOT NULL CHECK(source_kind IN ('natural_language','photo','order_image','correction')),

created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))) STRICT;

CREATE TABLE consumption_rules (id TEXT PRIMARY KEY NOT NULL,
content TEXT NOT NULL CHECK(length(trim(content))>0),
scope_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(scope_json) AND json_type(scope_json)='object'),
interpretation TEXT,
is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),

created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))) STRICT;

CREATE TABLE operations (id TEXT PRIMARY KEY NOT NULL,
request_key TEXT NOT NULL UNIQUE,
request_hash TEXT NOT NULL,
operation_type TEXT NOT NULL,
summary TEXT NOT NULL,
response_json TEXT NOT NULL CHECK(json_valid(response_json)),
undo_of_operation_id TEXT UNIQUE REFERENCES operations(id),
created_at TEXT NOT NULL,
CHECK(undo_of_operation_id IS NULL OR undo_of_operation_id<>id)) STRICT;

CREATE TABLE item_events (id TEXT PRIMARY KEY NOT NULL,
operation_id TEXT NOT NULL REFERENCES operations(id),
item_id TEXT REFERENCES items(id),
purchase_record_id TEXT REFERENCES purchase_records(id),
event_type TEXT NOT NULL CHECK(event_type IN
('recorded','received','usage_changed','condition_changed','location_changed','purpose_changed',
'split','quantity_corrected','sold','gifted','discarded','lost','returned','cancelled',
'depleted','sale_income_received','history_corrected','restored')),
quantity INTEGER CHECK(quantity>0),
amount_minor INTEGER CHECK(amount_minor>=0),
currency TEXT CHECK(currency IS NULL OR (length(currency)=3 AND currency=upper(currency))),
event_date TEXT,
date_precision TEXT NOT NULL DEFAULT 'unknown' CHECK(date_precision IN ('year','month','day','unknown')),
details_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(details_json) AND json_type(details_json)='object'),

created_at TEXT NOT NULL,
updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))
,
CHECK(item_id IS NOT NULL OR purchase_record_id IS NOT NULL),
CHECK(amount_minor IS NULL OR currency IS NOT NULL),
CHECK(amount_minor IS NULL OR event_type IN ('returned','cancelled','sale_income_received')),
CHECK((date_precision='unknown' AND event_date IS NULL) OR
(date_precision='year' AND event_date IS NOT NULL AND length(event_date)=4 AND event_date NOT GLOB '*[^0-9]*') OR
(date_precision='month' AND event_date IS NOT NULL AND event_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' AND substr(event_date,6,2) BETWEEN '01' AND '12') OR
(date_precision='day' AND event_date IS NOT NULL AND length(event_date)=10 AND coalesce(date(event_date,'+0 days')=event_date,0)))) STRICT;

CREATE TABLE operation_changes (operation_id TEXT NOT NULL REFERENCES operations(id),
sequence_no INTEGER NOT NULL CHECK(sequence_no>0),
entity_type TEXT NOT NULL CHECK(entity_type IN ('rooms','items','purchase_records','item_events','consumption_rules')),
entity_id TEXT NOT NULL,
change_kind TEXT NOT NULL CHECK(change_kind IN ('insert','update')),
before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
after_json TEXT NOT NULL CHECK(json_valid(after_json)),
expected_version INTEGER NOT NULL CHECK(expected_version>0),
PRIMARY KEY(operation_id,sequence_no),
UNIQUE(operation_id,entity_type,entity_id),
CHECK((change_kind='insert' AND before_json IS NULL) OR (change_kind='update' AND before_json IS NOT NULL))) STRICT;

CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY CHECK(version>0),
name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL) STRICT;


CREATE UNIQUE INDEX rooms_active_name ON rooms(name) WHERE is_void=0;
CREATE INDEX items_inventory ON items(is_void,ownership_status);
CREATE INDEX items_room ON items(room_id);
CREATE INDEX items_category ON items(category);
CREATE INDEX items_purchase ON items(purchase_record_id);
CREATE INDEX items_parent ON items(parent_item_id);
CREATE INDEX purchases_date ON purchase_records(purchase_date);
CREATE INDEX purchases_category ON purchase_records(category);
CREATE INDEX purchases_order ON purchase_records(merchant,external_order_id,external_line_id);
CREATE INDEX events_item ON item_events(item_id);
CREATE INDEX events_purchase ON item_events(purchase_record_id);
CREATE INDEX events_operation ON item_events(operation_id);
CREATE INDEX events_type_date ON item_events(event_type,event_date);
CREATE INDEX operations_date ON operations(created_at);
CREATE INDEX changes_entity ON operation_changes(entity_type,entity_id);
CREATE VIEW current_inventory AS SELECT * FROM items WHERE is_void=0 AND ownership_status='held';
