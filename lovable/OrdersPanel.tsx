import { useState, useEffect, useCallback } from "react";

// ── Config ────────────────────────────────────────────────────────────────────
const API_BASE = "https://cerca.remedia.ar";
const BO_KEY   = "TU_BO_KEY";   // ← reemplazá con tu clave real

function apiUrl(path: string) {
  return `${API_BASE}${path}${path.includes("?") ? "&" : "?"}key=${BO_KEY}`;
}

// ── Types ─────────────────────────────────────────────────────────────────────
interface Order {
  order_id: string;
  phone: string;
  sku_id: string;
  sku_nombre: string;
  cantidad: number;
  total: number;
  mp_payment_id: string;
  estado: "pendiente" | "preparado" | "retirado";
  pickup_code: string | null;
  created_at: string;
  updated_at: string;
}

// ── Confirm modal ─────────────────────────────────────────────────────────────
function ConfirmModal({
  open, title, description, productInfo, onConfirm, onCancel,
}: {
  open: boolean;
  title: string;
  description: string;
  productInfo: React.ReactNode;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 bg-black/60 z-50 flex items-center justify-center p-4"
      onClick={onCancel}
    >
      <div
        className="bg-gray-900 border border-gray-700 rounded-xl p-6 w-full max-w-sm shadow-2xl"
        onClick={e => e.stopPropagation()}
      >
        <h3 className="text-base font-bold text-white mb-2">{title}</h3>
        <p className="text-sm text-gray-400 mb-4">{description}</p>
        <div className="bg-gray-800 rounded-lg p-3 mb-5 text-sm">{productInfo}</div>
        <div className="flex gap-2 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg bg-gray-800 text-gray-400 hover:bg-gray-700 text-sm transition-colors"
          >
            Cancelar
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white font-semibold text-sm transition-colors"
          >
            Confirmar
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Order card ────────────────────────────────────────────────────────────────
function OrderCard({ order, onUpdate }: { order: Order; onUpdate: () => void }) {
  const [modal, setModal] = useState<"preparado" | "retirado" | null>(null);
  const [loading, setLoading] = useState(false);

  const hora = new Date(order.created_at).toLocaleTimeString("es-AR", {
    hour: "2-digit", minute: "2-digit",
  });
  const fecha = new Date(order.created_at).toLocaleDateString("es-AR", {
    day: "2-digit", month: "2-digit",
  });

  const estadoStyles = {
    pendiente: "bg-amber-950 text-amber-400",
    preparado: "bg-blue-950 text-blue-400",
    retirado:  "bg-emerald-950 text-emerald-400",
  };
  const estadoLabels = {
    pendiente: "🟡 Pendiente",
    preparado: "🔵 Preparado",
    retirado:  "✅ Retirado",
  };

  async function doAction(action: "preparado" | "retirado") {
    setLoading(true);
    setModal(null);
    try {
      await fetch(
        apiUrl(`/orders/api/${encodeURIComponent(order.order_id)}/${action}`),
        { method: "PATCH" }
      );
      onUpdate();
    } finally {
      setLoading(false);
    }
  }

  const productInfo = (
    <div className="space-y-1">
      <p className="font-semibold text-white">{order.sku_nombre}{order.cantidad > 1 ? ` x${order.cantidad}` : ""}</p>
      <p className="text-gray-400">Total: <span className="text-emerald-400 font-bold">${order.total.toLocaleString("es-AR", { minimumFractionDigits: 2 })}</span></p>
      <p className="text-gray-400">Cliente: <span className="font-mono text-gray-300">{order.phone}</span></p>
    </div>
  );

  return (
    <>
      <div className={`bg-gray-900 border rounded-xl p-4 space-y-3 ${
        order.estado === "retirado" ? "border-gray-800 opacity-60" : "border-gray-700"
      }`}>
        {/* Top row */}
        <div className="flex items-start justify-between gap-2">
          <div>
            <p className="font-semibold text-white text-sm">
              {order.sku_nombre}
              {order.cantidad > 1 && <span className="text-gray-400 ml-1">x{order.cantidad}</span>}
            </p>
            <p className="text-xs text-gray-500 font-mono mt-0.5">{order.order_id}</p>
          </div>
          <span className={`text-xs font-bold px-2 py-1 rounded-full shrink-0 ${estadoStyles[order.estado]}`}>
            {estadoLabels[order.estado]}
          </span>
        </div>

        {/* Info row */}
        <div className="flex items-center gap-4 text-sm">
          <span className="text-emerald-400 font-bold">
            ${order.total.toLocaleString("es-AR", { minimumFractionDigits: 2 })}
          </span>
          <span className="text-gray-500 font-mono text-xs">{order.phone}</span>
          <span className="text-gray-600 text-xs ml-auto">{fecha} {hora}</span>
        </div>

        {/* Pickup code */}
        {order.pickup_code && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-gray-500">Código retiro:</span>
            <span className="font-mono text-xl font-black text-blue-400 tracking-widest bg-blue-950/50 px-3 py-0.5 rounded-lg border border-blue-800">
              {order.pickup_code}
            </span>
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2">
          {order.estado === "pendiente" && (
            <button
              onClick={() => setModal("preparado")}
              disabled={loading}
              className="flex-1 bg-blue-600 hover:bg-blue-500 disabled:opacity-40 text-white
                         text-sm font-semibold py-2 rounded-lg transition-colors"
            >
              📦 Listo — avisar al cliente
            </button>
          )}
          {order.estado === "preparado" && (
            <button
              onClick={() => setModal("retirado")}
              disabled={loading}
              className="flex-1 bg-emerald-700 hover:bg-emerald-600 disabled:opacity-40 text-white
                         text-sm font-semibold py-2 rounded-lg transition-colors"
            >
              ✅ Confirmar retiro
            </button>
          )}
        </div>
      </div>

      {/* Modals */}
      <ConfirmModal
        open={modal === "preparado"}
        title="📦 Pedido listo"
        description="Se enviará un recordatorio con el código de retiro al cliente por WhatsApp."
        productInfo={productInfo}
        onConfirm={() => doAction("preparado")}
        onCancel={() => setModal(null)}
      />
      <ConfirmModal
        open={modal === "retirado"}
        title="✅ Confirmar Retiro"
        description="El cliente presentó el código y retiró el pedido."
        productInfo={
          <div className="space-y-1">
            <p className="font-semibold text-white">{order.sku_nombre}</p>
            {order.pickup_code && (
              <p className="text-gray-400">Código: <span className="font-mono text-blue-400 font-bold tracking-widest">{order.pickup_code}</span></p>
            )}
          </div>
        }
        onConfirm={() => doAction("retirado")}
        onCancel={() => setModal(null)}
      />
    </>
  );
}

// ── Main panel ────────────────────────────────────────────────────────────────
export default function OrdersPanel() {
  const [orders, setOrders] = useState<Order[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<"all" | "pendiente" | "preparado" | "retirado">("all");
  const [lastUpdate, setLastUpdate] = useState<Date | null>(null);

  const fetchOrders = useCallback(async () => {
    try {
      const r = await fetch(apiUrl("/orders/api/list"));
      if (!r.ok) return;
      setOrders(await r.json());
      setLastUpdate(new Date());
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchOrders(); }, [fetchOrders]);
  useEffect(() => {
    const t = setInterval(fetchOrders, 30000);
    return () => clearInterval(t);
  }, [fetchOrders]);

  const counts = {
    all:       orders.length,
    pendiente: orders.filter(o => o.estado === "pendiente").length,
    preparado: orders.filter(o => o.estado === "preparado").length,
    retirado:  orders.filter(o => o.estado === "retirado").length,
  };

  const filtered = filter === "all" ? orders : orders.filter(o => o.estado === filter);

  const FILTERS: { key: typeof filter; label: string }[] = [
    { key: "all",       label: "Todos" },
    { key: "pendiente", label: "🟡 Pendiente" },
    { key: "preparado", label: "🔵 Preparado" },
    { key: "retirado",  label: "✅ Retirado" },
  ];

  return (
    <div className="bg-gray-950 min-h-screen p-6 text-gray-200">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-xl font-bold text-white">🛍️ Pedidos</h1>
          {lastUpdate && (
            <p className="text-xs text-gray-500 mt-0.5">
              Actualizado: {lastUpdate.toLocaleTimeString("es-AR")}
            </p>
          )}
        </div>
        <button
          onClick={fetchOrders}
          className="text-sm px-4 py-2 rounded-lg bg-gray-800 hover:bg-gray-700 transition-colors"
        >
          ↺ Actualizar
        </button>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-3 gap-3 mb-5">
        {[
          { label: "Pendientes", val: counts.pendiente, color: "text-amber-400" },
          { label: "Preparados", val: counts.preparado, color: "text-blue-400" },
          { label: "Retirados",  val: counts.retirado,  color: "text-emerald-400" },
        ].map(s => (
          <div key={s.label} className="bg-gray-900 border border-gray-700 rounded-xl p-3 text-center">
            <div className="text-xs text-gray-500 uppercase tracking-wide mb-1">{s.label}</div>
            <div className={`text-3xl font-black ${s.color}`}>{s.val}</div>
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
            <span className="ml-1.5 opacity-60">{counts[f.key]}</span>
          </button>
        ))}
      </div>

      {/* Lista */}
      {loading ? (
        <div className="text-center text-gray-600 py-12">Cargando pedidos...</div>
      ) : filtered.length === 0 ? (
        <div className="text-center text-gray-600 py-12">
          {filter === "all" ? "Sin pedidos registrados aún" : `Sin pedidos en estado "${filter}"`}
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map(o => (
            <OrderCard key={o.order_id} order={o} onUpdate={fetchOrders} />
          ))}
        </div>
      )}
    </div>
  );
}
