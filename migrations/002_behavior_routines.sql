-- Schema version 2: user-authored routines and behavior rules.
CREATE TABLE routine_flows (
id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL CHECK(length(trim(name))>0), content TEXT NOT NULL CHECK(length(trim(content))>0),
weekdays_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(weekdays_json) AND json_type(weekdays_json)='array'),
applicability_text TEXT, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))) STRICT;
CREATE TABLE routine_stages (
id TEXT PRIMARY KEY NOT NULL, flow_id TEXT NOT NULL REFERENCES routine_flows(id),
name TEXT NOT NULL CHECK(length(trim(name))>0), content TEXT NOT NULL CHECK(length(trim(content))>0), position INTEGER NOT NULL CHECK(position>0),
reference_time TEXT CHECK(reference_time IS NULL OR
(length(reference_time)=5 AND reference_time GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(reference_time,1,2)<='23')),
is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1))) STRICT;
CREATE UNIQUE INDEX stages_position ON routine_stages(flow_id,position) WHERE is_void=0;
CREATE INDEX stages_flow ON routine_stages(flow_id,is_void,is_active,position);
CREATE INDEX flows_active ON routine_flows(is_void,is_active);
CREATE TABLE behavior_rules (
id TEXT PRIMARY KEY NOT NULL, content TEXT NOT NULL CHECK(length(trim(content))>0),
flow_id TEXT REFERENCES routine_flows(id), stage_id TEXT REFERENCES routine_stages(id),
kind TEXT NOT NULL CHECK(kind IN ('action','avoid')), behavior TEXT NOT NULL CHECK(length(trim(behavior))>0),
reason TEXT, alternative TEXT,
conditions_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(conditions_json) AND json_type(conditions_json)='object'),
interpretation TEXT, is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)), inactive_reason TEXT,
source_kind TEXT NOT NULL DEFAULT 'natural_language' CHECK(source_kind IN ('natural_language','note_import')),
source_label TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
is_void INTEGER NOT NULL DEFAULT 0 CHECK(is_void IN (0,1)), CHECK(stage_id IS NULL OR flow_id IS NOT NULL)) STRICT;
CREATE INDEX behavior_scope ON behavior_rules(flow_id,stage_id,is_void,is_active);
CREATE TABLE operation_changes_v2 (operation_id TEXT NOT NULL REFERENCES operations(id),
sequence_no INTEGER NOT NULL CHECK(sequence_no>0),
entity_type TEXT NOT NULL CHECK(entity_type IN ('rooms','items','purchase_records','item_events','consumption_rules','routine_flows','routine_stages','behavior_rules')),
entity_id TEXT NOT NULL,
change_kind TEXT NOT NULL CHECK(change_kind IN ('insert','update')),
before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
after_json TEXT NOT NULL CHECK(json_valid(after_json)),
expected_version INTEGER NOT NULL CHECK(expected_version>0),
PRIMARY KEY(operation_id,sequence_no),
UNIQUE(operation_id,entity_type,entity_id),
CHECK((change_kind='insert' AND before_json IS NULL) OR (change_kind='update' AND before_json IS NOT NULL))) STRICT;

INSERT INTO operation_changes_v2 SELECT * FROM operation_changes;
DROP TABLE operation_changes;
ALTER TABLE operation_changes_v2 RENAME TO operation_changes;
CREATE INDEX changes_entity ON operation_changes(entity_type,entity_id);
