import { useState, useEffect, useRef } from "react";

// ─── STAGE DEFINITIONS ────────────────────────────────────────────────────────
// Each stage defines which node IDs and edge IDs are "active" (highlighted)
const STAGES = [
  {
    id: 0,
    label: "Email Ingestion",
    color: "#6366f1",
    activeNodes: ["email_client", "orchestrator_llm"],
    activeEdges: ["e_email_orch"],
    description: "Client sends email → Orchestrator Agent receives and begins reasoning",
  },
  {
    id: 1,
    label: "RAI Gateway",
    color: "#f59e0b",
    activeNodes: ["orchestrator_llm", "rai_policy", "rai_guardrail", "rai_obs"],
    activeEdges: ["e_orch_rai", "e_rai_validated"],
    description: "All LLM calls pass through RAI Gateway — entitlement checks, I/O guardrails, observability",
  },
  {
    id: 2,
    label: "Planning",
    color: "#8b5cf6",
    activeNodes: ["orchestrator_llm", "agent_state", "action_router", "plan_parse", "plan_decompose", "plan_replan"],
    activeEdges: ["e_orch_plan", "e_orch_agentstate", "e_agentstate_router", "e_parse_decompose", "e_decompose_replan"],
    description: "Orchestrator plans: parse email, decompose task, maintain agent state, route to tools",
  },
  {
    id: 3,
    label: "MCP Tools",
    color: "#10b981",
    activeNodes: ["tool_db", "tool_entitle", "tool_ticket", "tool_email", "tool_sor", "tool_custom"],
    activeEdges: ["e_router_tools", "e_tools_memory"],
    description: "Action Router dispatches to MCP tools — DB queries, entitlement extraction, ticket creation, email, SOR",
  },
  {
    id: 4,
    label: "Memory",
    color: "#f97316",
    activeNodes: ["mem_short", "mem_episodic", "mem_long", "mem_records"],
    activeEdges: ["e_tools_memory", "e_mem_records"],
    description: "Memory System provides short-term context, episodic recall, and long-term client knowledge",
  },
  {
    id: 5,
    label: "Reflection",
    color: "#ef4444",
    activeNodes: ["eval_self", "eval_quality"],
    activeEdges: ["e_tools_eval", "e_self_quality", "e_quality_pass", "e_quality_fail"],
    description: "Agent self-critiques output. Quality Gate decides: PASS → Maker-Checker, FAIL → Re-plan",
  },
  {
    id: 6,
    label: "Maker-Checker",
    color: "#0ea5e9",
    activeNodes: ["maker_agent", "checker_agent", "inter_handoff", "out_sor", "out_email"],
    activeEdges: ["e_pass_maker", "e_maker_handoff", "e_handoff_checker", "e_checker_sor", "e_checker_email", "e_feedback"],
    description: "Maker prepares change → Checker validates → Update SoR + notify client → feedback loop",
  },
];

// ─── NODE DEFINITIONS ─────────────────────────────────────────────────────────
// position: [col, row] used for layout grid
const NODES = {
  // Row 0 – top
  email_client:    { label: "Email\nClient", type: "io",      col: 0,   row: 0 },
  orchestrator_llm:{ label: "LLM Core\n(via RAI Gateway)\nReasoning & Decision", type: "agent", col: 2, row: 0 },
  agent_state:     { label: "Agent State\nClient Context\n& Request Goals", type: "agent", col: 3, row: 0 },
  action_router:   { label: "Action Router\nTool Selection\n& Dispatch", type: "agent", col: 4, row: 0 },

  // RAI Gateway – top right
  rai_policy:      { label: "Policy/Model\nEntitlement\nChecking", type: "guard", col: 6, row: 0 },
  rai_guardrail:   { label: "Input/Output\nGuardrailing",          type: "guard", col: 7, row: 0 },
  rai_obs:         { label: "Observability",                        type: "guard", col: 6.5, row: 1 },

  // Planning – left mid
  plan_parse:      { label: "Parse & Extract\nEmail content\nClient Identity",       type: "plan", col: 0, row: 2 },
  plan_decompose:  { label: "Task Decomposition\nPrioritize entitlement\nsteps & fee lookup", type: "plan", col: 1, row: 2 },
  plan_replan:     { label: "Re-planning\nAdapt on failure",        type: "plan", col: 0, row: 3 },

  // MCP Tools – center
  tool_db:         { label: "DB Query\nClient Info\nMFees Lookup",  type: "tool", col: 2, row: 2 },
  tool_entitle:    { label: "Entitlement\nExtraction\nParse & Summarize", type: "tool", col: 3, row: 2 },
  tool_ticket:     { label: "Ticket\nCreation\nFor Maker review",   type: "tool", col: 4, row: 2 },
  tool_email:      { label: "Email Send\nClient Response",          type: "tool", col: 2, row: 3 },
  tool_sor:        { label: "System of Record\nUpdate\nWrite back", type: "tool", col: 3, row: 3 },
  tool_custom:     { label: "Custom Tools\nMCP Plugins",            type: "tool", col: 4, row: 3 },

  // Memory – bottom left
  mem_short:       { label: "Short-Term\nCurrent request\nWorking memory", type: "mem", col: 0, row: 4 },
  mem_episodic:    { label: "Episodic\nPast request\noutcomes",            type: "mem", col: 1, row: 4 },
  mem_long:        { label: "Long-Term\nClient entitlement\nKnowledge base", type: "mem", col: 0, row: 5 },
  mem_records:     { label: "Records\nDatastore",                          type: "db",  col: 1, row: 5 },

  // Reflection – center right
  eval_self:       { label: "Self-Critique\nVerify extraction\naccuracy & completeness", type: "eval", col: 5, row: 2 },
  eval_quality:    { label: "Quality Gate\nEntitlement validity\nSafety & Relevance",    type: "evalgate", col: 5, row: 3 },

  // Maker-Checker – right
  maker_agent:     { label: "Maker Agent\n(entitlement-maker)\nReviews & prepares\nchange", type: "multi", col: 6, row: 2 },
  inter_handoff:   { label: "Inter-Agent Handoff\nMessage Passing\n& Audit Trail",          type: "infra", col: 6, row: 3 },
  checker_agent:   { label: "Checker Agent\n(entitlement-checker)\nValidates & approves\nchange", type: "multi", col: 7, row: 2 },

  // Outputs – far right
  out_sor:         { label: "Update System\nof Record",           type: "output", col: 8, row: 2 },
  out_email:       { label: "Response Email\nto Client",          type: "output", col: 8, row: 3 },
};

// ─── EDGE DEFINITIONS ─────────────────────────────────────────────────────────
const EDGES = [
  { id: "e_email_orch",      from: "email_client",     to: "orchestrator_llm", label: "1. Receive Request" },
  { id: "e_orch_rai",        from: "orchestrator_llm", to: "rai_policy",       label: "LLM Calls" },
  { id: "e_rai_validated",   from: "rai_guardrail",    to: "orchestrator_llm", label: "Validated Response", dashed: true },
  { id: "e_orch_agentstate", from: "orchestrator_llm", to: "agent_state",      label: "" },
  { id: "e_agentstate_router",from:"agent_state",      to: "action_router",   label: "" },
  { id: "e_orch_plan",       from: "orchestrator_llm", to: "plan_parse",       label: "2. Plan" },
  { id: "e_parse_decompose", from: "plan_parse",       to: "plan_decompose",   label: "" },
  { id: "e_decompose_replan",from: "plan_decompose",   to: "plan_replan",      label: "on failure", dashed: true },
  { id: "e_router_tools",    from: "action_router",    to: "tool_db",          label: "3. Execute Tools" },
  { id: "e_tools_memory",    from: "tool_db",          to: "mem_short",        label: "Read/Write Memory", dashed: true },
  { id: "e_mem_records",     from: "mem_long",         to: "mem_records",      label: "" },
  { id: "e_tools_eval",      from: "tool_entitle",     to: "eval_self",        label: "4. Evaluate" },
  { id: "e_self_quality",    from: "eval_self",        to: "eval_quality",     label: "" },
  { id: "e_quality_pass",    from: "eval_quality",     to: "maker_agent",      label: "Pass → Maker" },
  { id: "e_quality_fail",    from: "eval_quality",     to: "plan_decompose",   label: "Fail → Re-plan", dashed: true },
  { id: "e_pass_maker",      from: "eval_quality",     to: "maker_agent",      label: "5. Pass" },
  { id: "e_maker_handoff",   from: "maker_agent",      to: "inter_handoff",    label: "Handoff to Checker" },
  { id: "e_handoff_checker", from: "inter_handoff",    to: "checker_agent",    label: "" },
  { id: "e_checker_sor",     from: "checker_agent",    to: "out_sor",          label: "Approve" },
  { id: "e_checker_email",   from: "checker_agent",    to: "out_email",        label: "8. Notify Client" },
  { id: "e_feedback",        from: "out_email",        to: "email_client",     label: "9. Client Feedback Loop", dashed: true },
];

// ─── STYLE MAP ────────────────────────────────────────────────────────────────
const TYPE_STYLE = {
  io:       { bg: "#1e3a5f", border: "#3b82f6", text: "#93c5fd", shape: "rounded" },
  agent:    { bg: "#1e1b4b", border: "#6366f1", text: "#c4b5fd", shape: "rect" },
  guard:    { bg: "#1c1208", border: "#d97706", text: "#fcd34d", shape: "rect" },
  plan:     { bg: "#1a0533", border: "#9333ea", text: "#d8b4fe", shape: "rect" },
  tool:     { bg: "#022c22", border: "#059669", text: "#6ee7b7", shape: "rect" },
  mem:      { bg: "#1c0a00", border: "#ea580c", text: "#fdba74", shape: "rect" },
  db:       { bg: "#1c1000", border: "#ca8a04", text: "#fde68a", shape: "cylinder" },
  eval:     { bg: "#1f0707", border: "#dc2626", text: "#fca5a5", shape: "rect" },
  evalgate: { bg: "#1f0707", border: "#dc2626", text: "#fca5a5", shape: "diamond" },
  multi:    { bg: "#0a1f2e", border: "#0284c7", text: "#7dd3fc", shape: "rect" },
  infra:    { bg: "#0f172a", border: "#475569", text: "#94a3b8", shape: "rect" },
  output:   { bg: "#022c0a", border: "#16a34a", text: "#86efac", shape: "rect" },
};

const COL_W = 148;
const ROW_H = 120;
const NODE_W = 130;
const NODE_H = 70;
const PAD_X = 24;
const PAD_Y = 24;

function nodePos(node) {
  return {
    x: PAD_X + node.col * COL_W,
    y: PAD_Y + node.row * ROW_H,
  };
}

function nodeCenterX(node) { return nodePos(node).x + NODE_W / 2; }
function nodeCenterY(node) { return nodePos(node).y + NODE_H / 2; }

// compute SVG size
const maxCol = Math.max(...Object.values(NODES).map(n => n.col));
const maxRow = Math.max(...Object.values(NODES).map(n => n.row));
const SVG_W = PAD_X * 2 + (maxCol + 1) * COL_W + 20;
const SVG_H = PAD_Y * 2 + (maxRow + 1) * ROW_H + 20;

function NodeBox({ id, node, active, stageColor, onClick }) {
  const s = TYPE_STYLE[node.type];
  const pos = nodePos(node);
  const isActive = active;
  const lines = node.label.split("\n");

  return (
    <g
      onClick={() => onClick(id)}
      style={{ cursor: "pointer" }}
    >
      {/* glow */}
      {isActive && (
        <rect
          x={pos.x - 5} y={pos.y - 5}
          width={NODE_W + 10} height={NODE_H + 10}
          rx={10} ry={10}
          fill="none"
          stroke={stageColor}
          strokeWidth={3}
          opacity={0.5}
          style={{ filter: `drop-shadow(0 0 8px ${stageColor})` }}
        />
      )}
      <rect
        x={pos.x} y={pos.y}
        width={NODE_W} height={NODE_H}
        rx={7} ry={7}
        fill={isActive ? s.bg : "#111"}
        stroke={isActive ? stageColor : s.border}
        strokeWidth={isActive ? 2.5 : 1.5}
        opacity={isActive ? 1 : 0.45}
        style={{ transition: "all 0.35s" }}
      />
      {lines.map((line, i) => (
        <text
          key={i}
          x={pos.x + NODE_W / 2}
          y={pos.y + 16 + i * 13}
          textAnchor="middle"
          fontSize={i === 0 ? 9.5 : 8.5}
          fontWeight={i === 0 ? 700 : 400}
          fill={isActive ? s.text : "#555"}
          style={{ transition: "fill 0.35s", fontFamily: "'IBM Plex Mono', monospace" }}
        >
          {line}
        </text>
      ))}
    </g>
  );
}

function EdgeLine({ edge, active, stageColor }) {
  const fromNode = NODES[edge.from];
  const toNode = NODES[edge.to];
  if (!fromNode || !toNode) return null;

  const x1 = nodeCenterX(fromNode);
  const y1 = nodeCenterY(fromNode);
  const x2 = nodeCenterX(toNode);
  const y2 = nodeCenterY(toNode);

  // simple straight line with slight curve
  const mx = (x1 + x2) / 2;
  const my = (y1 + y2) / 2;
  const d = `M ${x1} ${y1} Q ${mx} ${my} ${x2} ${y2}`;

  return (
    <g>
      <path
        d={d}
        fill="none"
        stroke={active ? stageColor : "#2a2a3a"}
        strokeWidth={active ? 2.5 : 1}
        strokeDasharray={edge.dashed ? "6,4" : "none"}
        markerEnd={`url(#arrow-${active ? "active" : "dim"})`}
        opacity={active ? 1 : 0.3}
        style={{ transition: "all 0.35s", filter: active ? `drop-shadow(0 0 4px ${stageColor})` : "none" }}
      />
      {edge.label && active && (
        <text
          x={mx} y={my - 6}
          textAnchor="middle"
          fontSize={7.5}
          fill={stageColor}
          fontWeight={600}
          style={{ fontFamily: "'IBM Plex Mono', monospace" }}
        >
          {edge.label}
        </text>
      )}
    </g>
  );
}

// ─── SUBGROUP BOXES ───────────────────────────────────────────────────────────
function SubgroupBox({ label, nodeIds, color, activeStageColor, isActiveGroup }) {
  const ns = nodeIds.map(id => NODES[id]).filter(Boolean);
  if (!ns.length) return null;
  const xs = ns.map(n => nodePos(n).x);
  const ys = ns.map(n => nodePos(n).y);
  const minX = Math.min(...xs) - 10;
  const minY = Math.min(...ys) - 18;
  const maxX = Math.max(...xs) + NODE_W + 10;
  const maxY = Math.max(...ys) + NODE_H + 10;
  return (
    <g>
      <rect
        x={minX} y={minY}
        width={maxX - minX} height={maxY - minY}
        rx={10} ry={10}
        fill={isActiveGroup ? color + "18" : "#ffffff05"}
        stroke={isActiveGroup ? color : "#2a2a3a"}
        strokeWidth={isActiveGroup ? 2 : 1}
        strokeDasharray="6,3"
        style={{ transition: "all 0.35s" }}
      />
      <text
        x={minX + 8} y={minY + 12}
        fontSize={8} fontWeight={700}
        fill={isActiveGroup ? color : "#444"}
        style={{ fontFamily: "'IBM Plex Mono', monospace", transition: "fill 0.35s" }}
      >
        {label}
      </text>
    </g>
  );
}

const SUBGROUPS = [
  { label: "ORCHESTRATOR AGENT", nodeIds: ["orchestrator_llm", "agent_state", "action_router"], color: "#6366f1" },
  { label: "RAI GATEWAY (LLM Access)", nodeIds: ["rai_policy", "rai_guardrail", "rai_obs"], color: "#f59e0b" },
  { label: "PLANNING MODULE", nodeIds: ["plan_parse", "plan_decompose", "plan_replan"], color: "#8b5cf6" },
  { label: "MCP SERVER — TOOLS", nodeIds: ["tool_db", "tool_entitle", "tool_ticket", "tool_email", "tool_sor", "tool_custom"], color: "#10b981" },
  { label: "MEMORY SYSTEM", nodeIds: ["mem_short", "mem_episodic", "mem_long", "mem_records"], color: "#f97316" },
  { label: "REFLECTION & EVALUATION", nodeIds: ["eval_self", "eval_quality"], color: "#ef4444" },
  { label: "MULTI-AGENT: MAKER-CHECKER WORKFLOW", nodeIds: ["maker_agent", "checker_agent", "inter_handoff"], color: "#0ea5e9" },
];

// ─── MAIN APP ─────────────────────────────────────────────────────────────────
export default function App() {
  const [activeStage, setActiveStage] = useState(null);
  const [autoPlay, setAutoPlay] = useState(false);
  const intervalRef = useRef(null);

  useEffect(() => {
    if (autoPlay) {
      intervalRef.current = setInterval(() => {
        setActiveStage(prev => {
          if (prev === null) return 0;
          if (prev >= STAGES.length - 1) { setAutoPlay(false); return prev; }
          return prev + 1;
        });
      }, 2200);
    } else {
      clearInterval(intervalRef.current);
    }
    return () => clearInterval(intervalRef.current);
  }, [autoPlay]);

  const stage = activeStage !== null ? STAGES[activeStage] : null;
  const activeNodes = stage ? new Set(stage.activeNodes) : new Set();
  const activeEdges = stage ? new Set(stage.activeEdges) : new Set();
  const stageColor = stage ? stage.color : "#6366f1";

  // which subgroups are active
  const activeSubgroups = new Set();
  if (stage) {
    SUBGROUPS.forEach(sg => {
      if (sg.nodeIds.some(id => activeNodes.has(id))) activeSubgroups.add(sg.label);
    });
  }

  return (
    <div style={{
      background: "#080812",
      minHeight: "100vh",
      fontFamily: "'IBM Plex Mono', 'Courier New', monospace",
      color: "#fff",
      padding: "0",
      display: "flex",
      flexDirection: "column",
    }}>
      {/* ── Header ── */}
      <div style={{
        padding: "20px 32px 12px",
        borderBottom: "1px solid #1a1a2e",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        flexWrap: "wrap",
        gap: 12,
      }}>
        <div>
          <div style={{ fontSize: 10, letterSpacing: 3, color: "#6366f1", textTransform: "uppercase", marginBottom: 2 }}>
            Demo Use Case · Private Markets MFees Extraction
          </div>
          <div style={{
            fontSize: 18, fontWeight: 800,
            background: "linear-gradient(90deg, #6366f1 0%, #0ea5e9 50%, #10b981 100%)",
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>
            Agentic AI Workflow
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <button
            onClick={() => { setAutoPlay(false); setActiveStage(null); }}
            style={btnStyle("#1e1e2e", "#333", activeStage === null ? "#fff" : "#888")}
          >Reset</button>
          <button
            onClick={() => setAutoPlay(v => !v)}
            style={btnStyle(autoPlay ? "#6366f1" : "#1e1e2e", autoPlay ? "#6366f1" : "#333", "#fff")}
          >{autoPlay ? "⏸ Pause" : "▶ Auto-Play"}</button>
        </div>
      </div>

      {/* ── Stage Selector ── */}
      <div style={{
        padding: "12px 32px",
        borderBottom: "1px solid #1a1a2e",
        display: "flex",
        gap: 6,
        flexWrap: "wrap",
        alignItems: "center",
      }}>
        <span style={{ fontSize: 9, color: "#444", marginRight: 6, textTransform: "uppercase", letterSpacing: 2 }}>Stages →</span>
        {STAGES.map((s, i) => (
          <button
            key={s.id}
            onClick={() => { setAutoPlay(false); setActiveStage(activeStage === i ? null : i); }}
            style={{
              padding: "5px 13px",
              borderRadius: 20,
              border: `1.5px solid ${activeStage === i ? s.color : "#2a2a2a"}`,
              background: activeStage === i ? s.color + "30" : "transparent",
              color: activeStage === i ? s.color : "#555",
              fontFamily: "inherit",
              fontSize: 10,
              fontWeight: 700,
              cursor: "pointer",
              transition: "all 0.2s",
              letterSpacing: 0.3,
            }}
          >
            {String(i + 1).padStart(2, "0")} {s.label}
          </button>
        ))}
      </div>

      {/* ── Description bar ── */}
      <div style={{
        padding: "8px 32px",
        minHeight: 32,
        borderBottom: "1px solid #111",
        fontSize: 11,
        color: stage ? stageColor : "#333",
        letterSpacing: 0.3,
        transition: "color 0.3s",
      }}>
        {stage ? `▸ ${stage.description}` : "Select a stage above or click any node — or hit Auto-Play"}
      </div>

      {/* ── Diagram ── */}
      <div style={{ flex: 1, overflow: "auto", padding: "16px 24px" }}>
        <svg
          width={SVG_W}
          height={SVG_H}
          style={{ display: "block", minWidth: SVG_W }}
        >
          <defs>
            <marker id="arrow-active" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">
              <path d="M0,0 L0,6 L8,3 z" fill={stageColor} />
            </marker>
            <marker id="arrow-dim" markerWidth="6" markerHeight="6" refX="5" refY="2.5" orient="auto">
              <path d="M0,0 L0,5 L6,2.5 z" fill="#2a2a3a" />
            </marker>
          </defs>

          {/* Subgroup boxes */}
          {SUBGROUPS.map(sg => (
            <SubgroupBox
              key={sg.label}
              label={sg.label}
              nodeIds={sg.nodeIds}
              color={sg.color}
              isActiveGroup={activeSubgroups.has(sg.label)}
            />
          ))}

          {/* Edges */}
          {EDGES.map(edge => (
            <EdgeLine
              key={edge.id}
              edge={edge}
              active={activeEdges.has(edge.id)}
              stageColor={stageColor}
            />
          ))}

          {/* Nodes */}
          {Object.entries(NODES).map(([id, node]) => (
            <NodeBox
              key={id}
              id={id}
              node={node}
              active={activeStage === null || activeNodes.has(id)}
              stageColor={stageColor}
              onClick={(nid) => {
                // find first stage containing this node
                const found = STAGES.findIndex(s => s.activeNodes.includes(nid));
                if (found !== -1) setActiveStage(found);
              }}
            />
          ))}
        </svg>
      </div>

      {/* ── Legend ── */}
      <div style={{
        padding: "10px 32px",
        borderTop: "1px solid #1a1a2e",
        display: "flex",
        flexWrap: "wrap",
        gap: 14,
        alignItems: "center",
      }}>
        {[
          ["#6366f1", "Planning / LLM / RAI"],
          ["#10b981", "Tool Execution / MCP"],
          ["#f97316", "Memory / Agent State"],
          ["#ef4444", "Reflection / Evaluation"],
          ["#f59e0b", "Guardrails / Safety"],
          ["#0ea5e9", "Multi-Agent (Maker/Checker)"],
          ["#16a34a", "Output"],
        ].map(([color, label]) => (
          <div key={label} style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <div style={{ width: 10, height: 10, borderRadius: 2, background: color }} />
            <span style={{ fontSize: 9, color: "#555", letterSpacing: 0.5 }}>{label}</span>
          </div>
        ))}
        <div style={{ marginLeft: "auto", display: "flex", gap: 16 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <svg width={28} height={8}><line x1="0" y1="4" x2="28" y2="4" stroke="#555" strokeWidth="1.5" /></svg>
            <span style={{ fontSize: 9, color: "#555" }}>Primary flow</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
            <svg width={28} height={8}><line x1="0" y1="4" x2="28" y2="4" stroke="#555" strokeWidth="1.5" strokeDasharray="4,3" /></svg>
            <span style={{ fontSize: 9, color: "#555" }}>Feedback / retry</span>
          </div>
        </div>
      </div>
    </div>
  );
}

function btnStyle(bg, border, color) {
  return {
    background: bg,
    border: `1px solid ${border}`,
    color,
    padding: "6px 16px",
    borderRadius: 6,
    fontSize: 11,
    fontWeight: 700,
    cursor: "pointer",
    fontFamily: "inherit",
    letterSpacing: 0.5,
  };
}
