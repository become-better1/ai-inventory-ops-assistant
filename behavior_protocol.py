"""Strict F13 MCP input schemas; independent from the existing consumption tools."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, create_model

Text = Annotated[str, Field(min_length=1, pattern=r'\S')]
ID = Annotated[str, Field(pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
Positive = Annotated[int, Field(gt=0)]
Day = Annotated[int, Field(ge=1, le=7)]
Clock = Annotated[str, Field(pattern=r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')]
CONFIG = ConfigDict(strict=True, extra='forbid')


class Strict(BaseModel):
    model_config = CONFIG


class TimeWindow(Strict):
    start: Clock
    end: Clock


class Conditions(Strict):
    locations: list[Text] = Field(default_factory=list)
    states: list[Text] = Field(default_factory=list)
    triggers: list[Text] = Field(default_factory=list)
    weekdays: list[Day] = Field(default_factory=list)
    time_windows: list[TimeWindow] = Field(default_factory=list)


FLOW = dict(name=(Text,...), content=(Text,...), weekdays=(list[Day],[]),
            applicability_text=(str|None,None), is_active=(bool,True))
STAGE = dict(name=(Text,...), content=(Text,...), reference_time=(Clock|None,None), is_active=(bool,True))
RULE = dict(content=(Text,...), kind=(Literal['action','avoid'],...), behavior=(Text,...),
            flow_id=(ID|None,None), stage_id=(ID|None,None), reason=(str|None,None),
            alternative=(str|None,None), conditions=(Conditions,Conditions()), interpretation=(str|None,None),
            is_active=(bool,True), inactive_reason=(str|None,None),
            source_kind=(Literal['natural_language','note_import'],'natural_language'), source_label=(str|None,None))
FlowInput = create_model('FlowInput', __config__=CONFIG, **FLOW)
StageInput = create_model('StageInput', __config__=CONFIG, **STAGE)
RuleInput = create_model('BehaviorRuleInput', __config__=CONFIG, **RULE)
# Defaults are only omission placeholders. exclude_unset preserves omitted vs explicit null.
FlowPatch = create_model('FlowPatch', __config__=CONFIG, **{k:(v[0],None) for k,v in FLOW.items()})
RulePatch = create_model('BehaviorRulePatch', __config__=CONFIG, **{k:(v[0],None) for k,v in RULE.items()})
Context = create_model('BehaviorContext', __config__=CONFIG,
    flow_id=(ID,None), stage_id=(ID,None), location=(Text,None), states=(list[Text],None),
    trigger=(Text,None), local_datetime=(Text,None), timezone=(Text,None))
PAGE = dict(limit=(Annotated[int,Field(ge=1,le=100)],50), cursor=(str|None,None))


def model(name, **fields):
    return create_model(name+'Request', __config__=CONFIG, **fields)


MODELS = {
    'routine_flows': model('routine_flows', active_only=(bool,False), **PAGE),
    'routine_stages': model('routine_stages', flow_id=(ID,...), include_inactive=(bool,False)),
    'behavior_rules': model('behavior_rules', rule_id=(ID|None,None), flow_id=(ID|None,None),
                            stage_id=(ID|None,None), query=(Text|None,None), active_only=(bool,False), **PAGE),
    'behavior_candidates': model('behavior_candidates', context=(Context,Context()),
                  candidate_ids=(Annotated[list[ID],Field(max_length=100)]|None,None), **PAGE),
    'create_behavior_rule': model('create_behavior_rule', request_key=(Text,...), rule=(RuleInput,...), confirmed_distinct=(bool,False)),
    'update_behavior_rule': model('update_behavior_rule', request_key=(Text,...),rule_id=(ID,...),expected_version=(Positive,...),changes=(RulePatch,...)),
    'delete_behavior_rule': model('delete_behavior_rule', request_key=(Text,...),rule_id=(ID,...),expected_version=(Positive,...)),
    'create_routine_flow': model('create_routine_flow',request_key=(Text,...),flow=(FlowInput,...),
                               stages=(Annotated[list[StageInput],Field(max_length=100)],[]),confirmed_distinct=(bool,False)),
    'update_routine_flow': model('update_routine_flow',request_key=(Text,...),flow_id=(ID,...),expected_version=(Positive,...),changes=(FlowPatch,...)),
}

DESCRIPTIONS = {
    'routine_flows': ('查询日常流程和适用日。阶段优先于参考时间；有下一页须继续查询，不能按时间推断完成。',True),
    'routine_stages': ('查询指定流程的有序阶段和版本；只读，不自动推进当前阶段或标记完成。',True),
    'behavior_rules': ('仅用于查看或维护准则及查ID/version。当前该做什么、想看手机等提醒必须使用behavior_candidates；未明确阶段先简短询问，不可据管理清单直接选睡前方案。query仅作关键词检索。',True),
    'behavior_candidates': ('用户问现在该做什么或想做某行为时必须优先使用此提醒工具。requires_stage_confirmation=true时只问next_question并等待，不能先给方案。unknown须补问或理解原文，分页未完继续查询；没有方案不编造。',True),
    'create_behavior_rule': ('仅用户明确制定长期行为准则时新增；保留原文。当天例外不写入，新增前核对疑似重复，重试沿用原键。',False),
    'update_behavior_rule': ('修改指定行为准则，先查询ID和版本。省略字段保留，null仅清空可空字段；conditions={}清空条件。is_active用于暂停或恢复；今天例外不改长期规则。',False),
    'delete_behavior_rule': ('用户明确要求时删除指定行为准则，先查询版本；逻辑删除，可用返回的operation_id撤销。',False),
    'create_routine_flow': ('创建用户明确制定的单个长期流程及有序阶段，返回阶段ID；不把阶段创建或建议当作执行完成。多项笔记批量导入使用preview_behavior_plan和apply_behavior_plan。',False),
    'update_routine_flow': ('按当前版本修改流程名称、原文、适用日或启停。停用流程不覆盖其中单条准则的启停状态。',False),
}

# Closed action variants: a caller cannot provide arbitrary methods, tables or columns.
StagePatch = create_model('StagePatch', __config__=CONFIG, **{k:(v[0],None) for k,v in STAGE.items()})
ImportStage = create_model('ImportStage', __config__=CONFIG, **STAGE, client_ref=(Text,None))
CreateFlowAction = model('CreateFlowAction',type=(Literal['create_flow'],...),client_ref=(Text,...),
    flow=(FlowInput,...),stages=(Annotated[list[ImportStage],Field(max_length=100)],[]),confirmed_distinct=(bool,False))
CreateRuleAction = model('CreateRuleAction',type=(Literal['create_rule'],...),client_ref=(Text,...),
    rule=(RuleInput,...),flow_ref=(Text,None),stage_ref=(Text,None),confirmed_distinct=(bool,False))
UpdateRuleAction = model('UpdateRuleAction',type=(Literal['update_rule'],...),rule_id=(ID,...),
    expected_version=(Positive,...),changes=(RulePatch,...))
DeleteRuleAction = model('DeleteRuleAction',type=(Literal['delete_rule'],...),rule_id=(ID,...),expected_version=(Positive,...))
EditStageAction = model('EditStageAction',type=(Literal['edit_stage'],...),stage_id=(ID,...),
    expected_version=(Positive,...),changes=(StagePatch,...))
InsertStageAction = model('InsertStageAction',type=(Literal['insert_stage'],...),client_ref=(Text,...),flow_id=(ID,...),
    expected_flow_version=(Positive,...),before_stage_id=(ID|None,None),stage=(StageInput,...))
ReorderAction = model('ReorderAction',type=(Literal['reorder_stages'],...),flow_id=(ID,...),
    expected_flow_version=(Positive,...),ordered_stage_ids=(Annotated[list[ID],Field(max_length=100)],...))
DeleteStageAction = model('DeleteStageAction',type=(Literal['delete_stage'],...),stage_id=(ID,...),
    expected_version=(Positive,...),cascade_rules=(bool,False))
DeleteFlowAction = model('DeleteFlowAction',type=(Literal['delete_flow'],...),flow_id=(ID,...),
    expected_version=(Positive,...),cascade_dependents=(bool,False))
Action = Annotated[CreateFlowAction|CreateRuleAction|UpdateRuleAction|DeleteRuleAction|EditStageAction|
                   InsertStageAction|ReorderAction|DeleteStageAction|DeleteFlowAction,Field(discriminator='type')]
MODELS.update({
    'preview_behavior_plan':model('preview_behavior_plan',actions=(Annotated[list[Action],Field(min_length=1,max_length=200)],...)),
    'apply_behavior_plan':model('apply_behavior_plan',request_key=(Text,...),preview_id=(Text,...),confirmed=(bool,False)),
})
DESCRIPTIONS.update({
    'preview_behavior_plan':('只读预览阶段编辑、插入、重排、删除及笔记批量导入；按client_ref引用同批新对象，展示原文、前后变化、级联影响和确认要求。不写正式数据，预览不等于已保存。',True),
    'apply_behavior_plan':('仅执行服务端预览ID，不接受替换动作；requires_confirmation=true时须落实用户对原范围的授权才传confirmed=true。整批原子写入，失败不部分保存；重试沿用原键，撤销使用返回的operation_id。',False),
})
