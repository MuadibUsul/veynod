"use client";

// An agent's face, drawn in code from its address: the same agent always looks
// the same wherever it appears (a list row, a seat at a computer, a chunk on
// the diagram). The hat says the role; closed eyes mean it is not connected.

const BODY = ["#7aa7ff", "#ff9f7a", "#8fd18a", "#f2c14e", "#c49bff", "#5fd3c6", "#ff8fb1", "#a3b1c6"];
const EARS = ["cat", "bear", "bunny", "antenna"] as const;

function hash(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

/** One agent, drawn in code from its address: the same agent always looks the same. */
export function AgentFace({ id, role: given, asleep = false, size = 24 }: {
  id: string;
  role?: string | null;
  asleep?: boolean;
  size?: number;
}) {
  const seed = hash(id);
  const body = BODY[seed % BODY.length];
  const ears = EARS[(seed >>> 3) % EARS.length];
  const role = (given ?? "").toUpperCase();
  return (
    <svg className={`agent-face${asleep ? " asleep" : ""}`} width={size} height={size * 1.12} viewBox="0 0 64 72" aria-hidden>
      <ellipse cx="32" cy="68" rx="15" ry="3.5" fill="rgba(0,0,0,.45)" />
      <g className="feet">
        <ellipse className="foot l" cx="24" cy="63" rx="6" ry="4" fill={body} style={{ filter: "brightness(.72)" }} />
        <ellipse className="foot r" cx="40" cy="63" rx="6" ry="4" fill={body} style={{ filter: "brightness(.72)" }} />
      </g>
      <g className="ears" fill={body}>
        {ears === "cat" && <><path d="M15 26 18 8 28 20Z" /><path d="M49 26 46 8 36 20Z" /></>}
        {ears === "bear" && <><circle cx="17" cy="20" r="7" /><circle cx="47" cy="20" r="7" /></>}
        {ears === "bunny" && <><ellipse cx="22" cy="12" rx="5" ry="12" /><ellipse cx="42" cy="12" rx="5" ry="12" /></>}
        {ears === "antenna" && <><path d="M32 18V8" stroke={body} strokeWidth="3" strokeLinecap="round" /><circle cx="32" cy="7" r="3.5" /></>}
      </g>
      <g className="arms" fill={body} style={{ filter: "brightness(.85)" }}>
        <ellipse className="arm l" cx="12" cy="44" rx="4.5" ry="7" />
        <ellipse className="arm r" cx="52" cy="44" rx="4.5" ry="7" />
      </g>
      <rect x="12" y="18" width="40" height="46" rx="20" fill={body} />
      <rect x="12" y="50" width="40" height="14" rx="7" fill="rgba(11,15,20,.28)" />
      <g className="face">
        {asleep ? (
          <>
            <path d="M22 37q4 3 8 0M34 37q4 3 8 0" fill="none" stroke="#0b0f14" strokeWidth="2" strokeLinecap="round" />
          </>
        ) : (
          <g className="eyes">
            <ellipse cx="25" cy="36" rx="4" ry="5" fill="#0b0f14" />
            <ellipse cx="39" cy="36" rx="4" ry="5" fill="#0b0f14" />
            <circle cx="26.4" cy="34.2" r="1.4" fill="#fff" />
            <circle cx="40.4" cy="34.2" r="1.4" fill="#fff" />
          </g>
        )}
        <circle cx="19" cy="43" r="2.6" fill="#ff8fb1" opacity=".45" />
        <circle cx="45" cy="43" r="2.6" fill="#ff8fb1" opacity=".45" />
        <path d="M29 44q1.5 1.6 3 0q1.5 1.6 3 0" fill="none" stroke="#0b0f14" strokeWidth="1.6" strokeLinecap="round" />
      </g>
      {/* The hat says the role. */}
      {role === "EXECUTOR" && (
        <g><path d="M15 24a17 13 0 0 1 34 0Z" fill="#f2c14e" /><rect x="11" y="22" width="42" height="4" rx="2" fill="#d9a520" /></g>
      )}
      {role === "AUDITOR" && (
        <g fill="none" stroke="#e5e7eb" strokeWidth="1.8"><circle cx="25" cy="36" r="6.5" /><circle cx="39" cy="36" r="6.5" /><path d="M31.5 36h1" /></g>
      )}
      {role === "ANALYST" && <ellipse cx="34" cy="19" rx="15" ry="5" fill="#7c5cff" transform="rotate(-10 34 19)" />}
      {role === "SCOUT" && (
        <g><path d="M16 23a16 11 0 0 1 32 0Z" fill="#5fd3c6" /><rect x="34" y="21" width="18" height="4" rx="2" fill="#3fb0a4" /></g>
      )}
      {(role === "SUPER_ADMIN" || role === "COMMANDER") && (
        <g fill="none" stroke="#e5e7eb" strokeWidth="2.4"><path d="M13 36a19 19 0 0 1 38 0" /><path d="M51 38q2 8-6 10" /><circle cx="44" cy="48" r="1.6" fill="#e5e7eb" /></g>
      )}
    </svg>
  );
}
