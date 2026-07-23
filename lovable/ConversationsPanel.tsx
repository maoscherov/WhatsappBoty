import { useState, useEffect, useRef, useCallback } from "react";

// ── Config ────────────────────────────────────────────────────────────────────
const API_BASE = "https://cerca.remedia.ar";
const BO_KEY   = "TU_BO_KEY";   // ← reemplazá con tu clave real (o usá env var)

function apiUrl(path: string) {
  return `${API_BASE}${path}${path.includes("?") ? "&" : "?"}key=${BO_KEY}`;
}

// ── Types ─────────────────────────────────────────────────────────────────────
interface Session {
  phone: string;
  estado: "idle" | "esperando_confirmacion" | "esperando_pago" | "pedido_confirmado" | "operador";
  pending_sku_nombre: string | null;
  pending_precio: number | null;
  pending_cantidad: number;
  mensajes: number;
  ultimo_mensaje: string | null;
}

interface Message {
  role: "user" | "assistant" | "operator";
  content: string;
}

// ── Estado badge ──────────────────────────────────────────────────────────────
const ESTADO_LABELS: Record<string, { label: string; className: string }> = {
  idle:                   { label: "Idle",             className: "bg-gray-800 text-gray-400" },
  esperando_confirmacion: { label: "Confirmación",     className: "bg-amber-950 text-amber-400" },
  esperando_pago:         { label: "Esperando pago",   className: "bg-emerald-950 text-emerald-400" },
  pedido_confirmado:      { label: "✅ Pago ok",        className: "bg-indigo-950 text-indigo-300" },
  operador:               { label: "🔴 En atención",   className: "bg-red-950 text-red-400 animate-pulse" },
};

function EstadoBadge({ estado }: { estado: string }) {
  const cfg = ESTADO_LABELS[estado] ?? { label: estado, className: "bg-gray-800 text-gray-400" };
  return (
    <span className={`inline-block text-xs font-semibold px-2 py-0.5 rounded-full ${cfg.className}`}>
      {cfg.label}
    </span>
  );
}

// ── Conversation ──────────────────────────────────────────────────────────────
function Conversation({ phone, isOperator }: { phone: string; isOperator: boolean }) {
  const [history, setHistory] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchHistory = useCallback(async () => {
    const r = await fetch(apiUrl(`/bo/session/${encodeURIComponent(phone)}`));
    if (!r.ok) return;
    const d = await r.json();
    setHistory(d.history ?? []);
  }, [phone]);

  useEffect(() => { fetchHistory(); }, [fetchHistory]);

  // Auto-refresh cada 8s cuando el operador está activo
  useEffect(() => {
    if (!isOperator) return;
    const t = setInterval(fetchHistory, 8000);
    return () => clearInterval(t);
  }, [isOperator, fetchHistory]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [history]);

  const roleStyle: Record<string, string> = {
    user:      "text-blue-400",
    assistant: "text-emerald-400",
    operator:  "text-red-400",
  };
  const roleLabel: Record<string, string> = {
    user:      "👤 Usuario",
    assistant: "🤖 Remedia",
    operator:  "🧑‍💼 Operador",
  };

  async function sendMessage() {
    if (!text.trim() || sending) return;
    setSending(true);
    try {
      const r = await fetch(apiUrl(`/bo/session/${encodeURIComponent(phone)}/message`), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: text.trim() }),
      });
      if (r.ok) {
        setText("");
        await fetchHistory();
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="border border-gray-700 rounded-lg overflow-hidden">
      {/* Historial */}
      <div className="max-h-72 overflow-y-auto bg-gray-950 p-2 space-y-1">
        {history.length === 0 && (
          <p className="text-gray-600 text-sm text-center py-4">Sin historial</p>
        )}
        {history.map((m, i) => (
          <div key={i} className="flex gap-2 text-sm py-1 border-b border-gray-800 last:border-0">
            <span className={`font-bold min-w-[90px] shrink-0 text-xs pt-0.5 ${roleStyle[m.role] ?? "text-gray-400"}`}>
              {roleLabel[m.role] ?? m.role}
            </span>
            <span className="text-gray-300 whitespace-pre-wrap">{m.content}</span>
          </div>
        ))}
        <div ref={bottomRef} />
      </div>

      {/* Panel de operador */}
      {isOperator && (
        <div className="bg-red-950/30 border-t border-red-900/50 p-3 space-y-2">
          <p className="text-xs font-bold text-red-400 uppercase tracking-wide">
            🔴 Modo operador — bot silenciado
          </p>
          <div className="flex gap-2">
            <textarea
              className="flex-1 bg-gray-900 border border-gray-700 rounded-md p-2 text-sm text-gray-200
                         resize-none outline-none focus:border-red-500 placeholder-gray-600"
              rows={2}
              placeholder="Escribí tu mensaje... (Ctrl+Enter para enviar)"
              value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") sendMessage(); }}
            />
            <button
              onClick={sendMessage}
              disabled={sending || !text.trim()}
              className="bg-red-600 hover:bg-red-500 disabled:opacity-40 disabled:cursor-not-allowed
                         text-white text-sm font-semibold px-4 rounded-md transition-colors"
            >
              {sending ? "..." : "Enviar"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Session row ───────────────────────────────────────────────────────────────
function SessionRow({ session, onUpdate }: { session: Session; onUpdate: () => void }) {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);

  const isOperator = session.estado === "operador";

  async function takeover() {
    setLoading(true);
    await fetch(apiUrl(`/bo/session/${encodeURIComponent(session.phone)}/takeover`), { method: "POST" });
    setLoading(false);
    setExpanded(true);
    onUpdate();
  }

  async function release() {
    setLoading(true);
    await fetch(apiUrl(`/bo/session/${encodeURIComponent(session.phone)}/release`), { method: "POST" });
    setLoading(false);
    onUpdate();
  }

  async function closeSession() {
    if (!confirm("¿Cerrar esta conversación? Se quita de la lista de conversaciones activas.")) return;
    setLoading(true);
    await fetch(apiUrl(`/bo/session/${encodeURIComponent(session.phone)}/close`), { method: "POST" });
    setLoading(false);
    onUpdate();
  }

  return (
    <div className={`border rounded-lg overflow-hidden transition-colors ${isOperator ? "border-red-800" : "border-gray-700"}`}>
      {/* Header row */}
      <div
        className="flex items-center gap-3 p-3 cursor-pointer hover:bg-gray-800/50 bg-gray-900"
        onClick={() => setExpanded(e => !e)}
      >
        <span className="font-mono font-bold text-sm text-gray-200">{session.phone}</span>
        <EstadoBadge estado={session.estado} />

        {session.pending_sku_nombre && (
          <span className="text-xs text-gray-400 truncate max-w-[180px]">
            {session.pending_sku_nombre}
            {session.pending_cantidad > 1 ? ` x${session.pending_cantidad}` : ""}
          </span>
        )}

        <span className="text-xs text-gray-600 ml-auto shrink-0">{session.mensajes} msgs</span>

        {/* Acciones */}
        <div className="flex gap-2" onClick={e => e.stopPropagation()}>
          {isOperator ? (
            <button
              onClick={release}
              disabled={loading}
              className="text-xs px-3 py-1 rounded border border-gray-600 text-gray-400
                         hover:border-emerald-500 hover:text-emerald-400 transition-colors"
            >
              🤖 Devolver al bot
            </button>
          ) : (
            <button
              onClick={takeover}
              disabled={loading}
              className="text-xs px-3 py-1 rounded border border-gray-600 text-gray-400
                         hover:border-red-500 hover:text-red-400 transition-colors"
            >
              🙋 Tomar
            </button>
          )}
          <button
            onClick={closeSession}
            disabled={loading}
            title="Cerrar conversación"
            className="text-xs px-3 py-1 rounded border border-gray-600 text-gray-400
                       hover:border-red-500 hover:text-red-400 transition-colors"
          >
            ✖ Cerrar
          </button>
        </div>

        <span className="text-gray-600 text-xs">{expanded ? "▲" : "▼"}</span>
      </div>

      {/* Último mensaje preview */}
      {!expanded && session.ultimo_mensaje && (
        <div className="px-3 pb-2 text-xs text-gray-500 truncate bg-gray-900">
          {session.ultimo_mensaje}
        </div>
      )}

      {/* Conversación expandida */}
      {expanded && (
        <div className="p-3 bg-gray-950">
          <Conversation phone={session.phone} isOperator={isOperator} />
        </div>
      )}
    </div>
  );
}

// Beep de notificación (Web Audio — sin archivos externos, compatible con CSP)
function playBeep() {
  try {
    const Ctx = window.AudioContext || (window as any).webkitAudioContext;
    const ctx = new Ctx();
    const beep = (freq: number, start: number, dur: number) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.connect(g); g.connect(ctx.destination);
      o.type = "sine";
      o.frequency.value = freq;
      g.gain.setValueAtTime(0.0001, ctx.currentTime + start);
      g.gain.exponentialRampToValueAtTime(0.2, ctx.currentTime + start + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
      o.start(ctx.currentTime + start);
      o.stop(ctx.currentTime + start + dur);
    };
    // Dos tonos ascendentes (tipo "ding-dong")
    beep(660, 0, 0.25);
    beep(880, 0.22, 0.35);
  } catch { /* autoplay bloqueado hasta la primera interacción */ }
}

// ── Main panel ────────────────────────────────────────────────────────────────
export default function ConversationsPanel() {
  const [sessions, setSessions] = useState<Session[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<string>("all");
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);
  const [muted, setMuted] = useState(false);

  const prevOperators = useRef<Set<string>>(new Set());
  const initialized = useRef(false);

  const fetchSessions = useCallback(async () => {
    try {
      const r = await fetch(apiUrl("/bo/sessions"));
      if (!r.ok) return;
      const data: Session[] = await r.json();
      setSessions(data);
      setLastUpdate(new Date());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchSessions(); }, [fetchSessions]);

  // Sonido cuando una conversación NUEVA pasa a modo operador (derivación)
  useEffect(() => {
    const current = new Set(
      sessions.filter(s => s.estado === "operador").map(s => s.phone)
    );
    if (initialized.current) {
      const nuevos = [...current].filter(p => !prevOperators.current.has(p));
      if (nuevos.length > 0 && !muted) playBeep();
    }
    prevOperators.current = current;
    initialized.current = true;
  }, [sessions, muted]);

  // Refresh dinámico: más rápido si hay operador activo
  useEffect(() => {
    const hasOperator = sessions.some(s => s.estado === "operador");
    const interval = setInterval(fetchSessions, hasOperator ? 8000 : 30000);
    return () => clearInterval(interval);
  }, [sessions, fetchSessions]);

  const FILTERS = [
    { key: "all",      label: "Todas" },
    { key: "operador", label: "🔴 En atención" },
    { key: "esperando_confirmacion", label: "🟡 Pendientes" },
    { key: "idle",     label: "Idle" },
  ];

  const filtered = filter === "all" ? sessions : sessions.filter(s => s.estado === filter);

  return (
    <div className="bg-gray-950 min-h-screen p-6 text-gray-200">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-white">💬 Conversaciones</h1>
          {lastUpdate && (
            <p className="text-xs text-gray-500 mt-0.5">
              Actualizado: {lastUpdate.toLocaleTimeString("es-AR")}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => { setMuted(m => !m); if (muted) playBeep(); }}
            title={muted ? "Sonido desactivado" : "Sonido activado (probar)"}
            className={`text-sm px-3 py-2 rounded-lg border transition-colors ${
              muted
                ? "border-gray-700 bg-gray-800 text-gray-500"
                : "border-emerald-700 bg-emerald-950 text-emerald-400"
            }`}
          >
            {muted ? "🔕" : "🔔"}
          </button>
          <button
            onClick={fetchSessions}
            className="text-sm px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 transition-colors"
          >
            ↺ Actualizar
          </button>
        </div>
      </div>

      {/* Stats pills */}
      <div className="flex gap-3 mb-5 flex-wrap">
        {[
          { label: "Activas",    val: sessions.length,                                    color: "text-gray-300" },
          { label: "Operador",   val: sessions.filter(s => s.estado === "operador").length, color: "text-red-400" },
          { label: "Pendientes", val: sessions.filter(s => s.estado === "esperando_confirmacion").length, color: "text-amber-400" },
        ].map(s => (
          <div key={s.label} className="bg-gray-900 border border-gray-700 rounded-lg px-4 py-2">
            <div className="text-xs text-gray-500 uppercase tracking-wide">{s.label}</div>
            <div className={`text-2xl font-bold ${s.color}`}>{s.val}</div>
          </div>
        ))}
      </div>

      {/* Filtros */}
      <div className="flex gap-2 mb-4 flex-wrap">
        {FILTERS.map(f => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={`text-xs px-3 py-1.5 rounded-lg border transition-colors ${
              filter === f.key
                ? "border-blue-500 bg-blue-950 text-blue-300"
                : "border-gray-700 text-gray-400 hover:border-gray-500"
            }`}
          >
            {f.label}
            <span className="ml-1.5 opacity-60">
              {f.key === "all" ? sessions.length : sessions.filter(s => s.estado === f.key).length}
            </span>
          </button>
        ))}
      </div>

      {/* Lista */}
      {loading ? (
        <div className="text-center text-gray-600 py-12">Cargando conversaciones...</div>
      ) : filtered.length === 0 ? (
        <div className="text-center text-gray-600 py-12">Sin conversaciones activas</div>
      ) : (
        <div className="space-y-2">
          {filtered.map(s => (
            <SessionRow key={s.phone} session={s} onUpdate={fetchSessions} />
          ))}
        </div>
      )}
    </div>
  );
}
