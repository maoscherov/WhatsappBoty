import { useState, useEffect, useCallback, useRef } from "react";

const API_BASE = "https://cerca.remedia.ar";
const BO_KEY   = "TU_BO_KEY";

function apiUrl(path: string) {
  return `${API_BASE}${path}${path.includes("?") ? "&" : "?"}key=${BO_KEY}`;
}

// ── Types ─────────────────────────────────────────────────────────────────────
interface DaySchedule {
  open: string;
  close: string;
  active: boolean;
}
interface BotConfig {
  send_images: string;
  pickup_minutes: string;
}
interface BusinessHours {
  enabled: boolean;
  closed_message: string;
  schedule: Record<string, DaySchedule>;
}

const DAY_LABELS: Record<string, string> = {
  mon: "Lunes", tue: "Martes", wed: "Miércoles", thu: "Jueves",
  fri: "Viernes", sat: "Sábado", sun: "Domingo",
};
const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

// ── SKU Import section ────────────────────────────────────────────────────────
function SKUSection() {
  const [info, setInfo]       = useState<{ total: number; csv_path: string } | null>(null);
  const [uploading, setUploading] = useState(false);
  const [result, setResult]   = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const fetchInfo = useCallback(async () => {
    const r = await fetch(apiUrl("/bo/sku/info"));
    if (r.ok) setInfo(await r.json());
  }, []);

  useEffect(() => { fetchInfo(); }, [fetchInfo]);

  async function handleUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploading(true);
    setResult(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await fetch(apiUrl("/bo/sku/import"), { method: "POST", body: form });
      const d = await r.json();
      if (r.ok) {
        setResult(`✅ Importados ${d.total} SKUs correctamente`);
        fetchInfo();
      } else {
        setResult(`❌ Error: ${d.detail}`);
      }
    } catch {
      setResult("❌ Error de red");
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-white">📦 Catálogo SKU</h2>
        {info && (
          <span className="text-sm text-gray-400">
            <span className="text-emerald-400 font-bold">{info.total.toLocaleString()}</span> productos cargados
          </span>
        )}
      </div>

      <div className="bg-gray-800 rounded-xl p-4 space-y-3">
        <p className="text-sm text-gray-400">
          Subí un CSV para reemplazar el catálogo actual. El bot lo usa inmediatamente sin necesidad de reiniciar.
        </p>
        <div className="flex items-center gap-3">
          <label className={`flex-1 flex items-center justify-center gap-2 px-4 py-3 rounded-lg border-2
            border-dashed cursor-pointer transition-colors text-sm font-medium
            ${uploading
              ? "border-gray-600 text-gray-600 cursor-not-allowed"
              : "border-gray-600 hover:border-blue-500 text-gray-400 hover:text-blue-400"
            }`}>
            <input
              ref={fileRef}
              type="file"
              accept=".csv"
              className="hidden"
              onChange={handleUpload}
              disabled={uploading}
            />
            {uploading ? "⏳ Importando..." : "📄 Seleccionar CSV"}
          </label>
        </div>
        {result && (
          <p className={`text-sm font-medium ${result.startsWith("✅") ? "text-emerald-400" : "text-red-400"}`}>
            {result}
          </p>
        )}
        {info && (
          <p className="text-xs text-gray-600 font-mono">{info.csv_path}</p>
        )}
      </div>
    </div>
  );
}

// ── Business Hours section ────────────────────────────────────────────────────
function HoursSection() {
  const [hours, setHours]       = useState<BusinessHours | null>(null);
  const [config, setConfig]     = useState<BotConfig | null>(null);
  const [saving, setSaving]     = useState(false);
  const [saved, setSaved]       = useState(false);

  useEffect(() => {
    fetch(apiUrl("/bo/config/hours")).then(r => r.json()).then(setHours);
    fetch(apiUrl("/bo/config")).then(r => r.json()).then(setConfig);
  }, []);

  async function save() {
    if (!hours) return;
    setSaving(true);
    await Promise.all([
      fetch(apiUrl("/bo/config/hours"), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(hours),
      }),
      config && fetch(apiUrl("/bo/config"), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pickup_minutes: config.pickup_minutes }),
      }),
    ]);
    setSaving(false);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  }

  function updateDay(day: string, field: keyof DaySchedule, value: string | boolean) {
    if (!hours) return;
    setHours({
      ...hours,
      schedule: {
        ...hours.schedule,
        [day]: { ...hours.schedule[day], [field]: value },
      },
    });
  }

  if (!hours || !config) return <div className="text-gray-600 text-sm">Cargando...</div>;

  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold text-white">🕐 Horarios de atención</h2>

      <div className="bg-gray-800 rounded-xl p-4 space-y-4">
        {/* Tiempo estimado */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-white">Tiempo estimado de retiro</p>
            <p className="text-xs text-gray-500">Aparece en los mensajes de confirmación</p>
          </div>
          <div className="flex items-center gap-2">
            <input
              type="number"
              min={5}
              max={480}
              step={5}
              value={config.pickup_minutes}
              onChange={e => setConfig({ ...config, pickup_minutes: e.target.value })}
              className="w-20 bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5
                         text-sm text-gray-200 text-center outline-none focus:border-blue-500"
            />
            <span className="text-sm text-gray-400">min</span>
          </div>
        </div>

        <div className="border-t border-gray-700" />

        {/* Toggle global */}
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-white">Control de horario activo</p>
            <p className="text-xs text-gray-500">Si está desactivado, el bot responde siempre</p>
          </div>
          <button
            onClick={() => setHours({ ...hours, enabled: !hours.enabled })}
            className={`w-12 h-6 rounded-full transition-colors relative ${
              hours.enabled ? "bg-emerald-600" : "bg-gray-600"
            }`}
          >
            <span className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-transform ${
              hours.enabled ? "translate-x-6" : "translate-x-0.5"
            }`} />
          </button>
        </div>

        {/* Horario por día */}
        {hours.enabled && (
          <>
            <div className="border-t border-gray-700 pt-3 space-y-2">
              {DAYS.map(day => {
                const cfg = hours.schedule[day] ?? { open: "09:00", close: "18:00", active: false };
                return (
                  <div key={day} className="flex items-center gap-3">
                    <button
                      onClick={() => updateDay(day, "active", !cfg.active)}
                      className={`w-9 h-5 rounded-full transition-colors relative shrink-0 ${
                        cfg.active ? "bg-emerald-600" : "bg-gray-600"
                      }`}
                    >
                      <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                        cfg.active ? "translate-x-4" : "translate-x-0.5"
                      }`} />
                    </button>

                    <span className={`text-sm w-24 shrink-0 ${cfg.active ? "text-white" : "text-gray-600"}`}>
                      {DAY_LABELS[day]}
                    </span>

                    {cfg.active ? (
                      <div className="flex items-center gap-2 text-sm">
                        <input
                          type="time"
                          value={cfg.open}
                          onChange={e => updateDay(day, "open", e.target.value)}
                          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-200 text-xs"
                        />
                        <span className="text-gray-500">—</span>
                        <input
                          type="time"
                          value={cfg.close}
                          onChange={e => updateDay(day, "close", e.target.value)}
                          className="bg-gray-900 border border-gray-700 rounded px-2 py-1 text-gray-200 text-xs"
                        />
                      </div>
                    ) : (
                      <span className="text-xs text-gray-600">Cerrado</span>
                    )}
                  </div>
                );
              })}
            </div>

            {/* Mensaje de cierre */}
            <div className="border-t border-gray-700 pt-3 space-y-2">
              <label className="text-xs text-gray-500 uppercase tracking-wide">Mensaje fuera de horario</label>
              <textarea
                rows={2}
                value={hours.closed_message}
                onChange={e => setHours({ ...hours, closed_message: e.target.value })}
                className="w-full bg-gray-900 border border-gray-700 rounded-lg px-3 py-2
                           text-sm text-gray-200 resize-none outline-none focus:border-blue-500"
              />
            </div>
          </>
        )}

        <button
          onClick={save}
          disabled={saving}
          className={`w-full py-2.5 rounded-lg text-sm font-semibold transition-colors ${
            saved
              ? "bg-emerald-700 text-white"
              : "bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white"
          }`}
        >
          {saved ? "✅ Guardado" : saving ? "Guardando..." : "Guardar horarios"}
        </button>
      </div>
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────
export default function SettingsPanel() {
  return (
    <div className="bg-gray-950 min-h-screen p-6 text-gray-200 space-y-8 max-w-2xl mx-auto">
      <h1 className="text-xl font-bold text-white">⚙️ Configuración</h1>
      <SKUSection />
      <div className="border-t border-gray-800" />
      <HoursSection />
    </div>
  );
}
