import { useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  ArrowDownToLine,
  BarChart3,
  Bot,
  Check,
  CheckCircle2,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  ExternalLink,
  Gauge,
  Globe2,
  Home,
  Link2,
  ListChecks,
  OctagonX,
  Pencil,
  Play,
  Plus,
  Radio,
  RefreshCw,
  RotateCw,
  Save,
  ServerCog,
  Settings,
  ShieldCheck,
  Square,
  Trash2,
  TrendingUp,
  WalletCards,
  X,
  XCircle,
  Zap,
} from "lucide-react";
import "./styles.css";

const bootstrapNode = document.getElementById("ghost-clicker-bootstrap");
const initial = JSON.parse(bootstrapNode?.textContent || "{}");
bootstrapNode?.remove();

const navigation = [
  { id: "home", label: "Accueil", icon: Home },
  { id: "targets", label: "URLs", icon: Link2 },
  { id: "gateways", label: "Réseau", icon: ServerCog },
  { id: "results", label: "Résultats", icon: BarChart3 },
  { id: "settings", label: "Réglages", icon: Settings },
];

const tabsWithPoll = new Set(["home", "results"]);

function moneyForClicks(clicks) {
  const blocks = Math.floor(Math.max(0, Number(clicks) || 0) / 250);
  return Math.min(blocks, 2) * 12 + Math.max(blocks - 2, 0) * 8;
}

function nextMilestone(clicks) {
  const value = Math.max(0, Number(clicks) || 0);
  const completed = Math.floor(value / 250);
  const inBlock = value % 250;
  return {
    remaining: inBlock === 0 ? 250 : 250 - inBlock,
    progress: (inBlock / 250) * 100,
    reward: completed < 2 ? 12 : 8,
  };
}

function number(value) {
  return new Intl.NumberFormat("fr-FR").format(Number(value) || 0);
}

function dateTime(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value;
  return new Intl.DateTimeFormat("fr-FR", {
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  }).format(parsed);
}

function duration(milliseconds) {
  const total = Number(milliseconds) || 0;
  if (total < 1000) return `${total} ms`;
  return `${(total / 1000).toFixed(1).replace(".", ",")} s`;
}

function reportResults(reports) {
  return (reports || []).flatMap((report) =>
    (report.results || []).map((result) => ({
      ...result,
      run_id: report.run_id,
      source: report.source,
      execution_mode: report.execution_mode,
    })),
  );
}

async function api(path, { method = "GET", body } = {}) {
  const response = await fetch(path, {
    method,
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(method === "GET" ? {} : { "X-CSRF-Token": initial.csrfToken }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : { message: await response.text() };
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.message || "La demande a échoué.");
  }
  return payload;
}

function App() {
  const [activeTab, setActiveTab] = useState("home");
  const [run, setRun] = useState(initial.run || {});
  const [targets, setTargets] = useState(initial.targets || []);
  const [gateways, setGateways] = useState(initial.gateways || []);
  const [reports, setReports] = useState(initial.reports || []);
  const [settings, setSettingsState] = useState(initial.settings || {});
  const [database, setDatabase] = useState(initial.database || {});
  const [messages, setMessages] = useState(initial.messages || []);
  const [toast, setToast] = useState(null);
  const [pending, setPending] = useState(false);

  const showToast = useCallback((message, tone = "success") => {
    setToast({ message, tone });
    window.setTimeout(() => setToast(null), 3500);
  }, []);

  const refreshStatus = useCallback(async (quiet = true) => {
    try {
      const payload = await api("/api/state");
      setRun(payload.run || {});
      setTargets(payload.targets || []);
      setGateways(payload.gateways || []);
      setReports(payload.reports || []);
      setSettingsState(payload.settings || {});
      setDatabase(payload.database || {});
    } catch (error) {
      if (!quiet) showToast(error.message, "error");
    }
  }, [showToast]);

  useEffect(() => {
    if (!tabsWithPoll.has(activeTab) && !run.running) return undefined;
    const identifier = window.setInterval(() => refreshStatus(true), 3000);
    return () => window.clearInterval(identifier);
  }, [activeTab, run.running, refreshStatus]);

  const mutate = async (path, options, callback) => {
    setPending(true);
    try {
      const payload = await api(path, options);
      if (callback) callback(payload);
      showToast(payload.message || "Modification enregistrée.");
      await refreshStatus(true);
      return payload;
    } catch (error) {
      showToast(error.message, "error");
      return null;
    } finally {
      setPending(false);
    }
  };

  const props = {
    run,
    targets,
    gateways,
    reports,
    settings,
    database,
    pending,
    mutate,
    refreshStatus,
    showToast,
    setTargets,
    setGateways,
    setReports,
    setSettingsState,
    goTo: setActiveTab,
  };

  return (
    <div className="app-shell">
      <Sidebar activeTab={activeTab} setActiveTab={setActiveTab} database={database} />
      <main className="main-canvas">
        <MobileHeader activeTab={activeTab} />
        {messages.length > 0 && (
          <div className="message-stack" role="alert">
            {messages.map((message) => (
              <div className="message" key={message}>
                <XCircle size={17} /> {message}
                <button onClick={() => setMessages((items) => items.filter((item) => item !== message))} aria-label="Fermer">
                  <X size={16} />
                </button>
              </div>
            ))}
          </div>
        )}
        {activeTab === "home" && <Dashboard {...props} />}
        {activeTab === "targets" && <Targets {...props} />}
        {activeTab === "gateways" && <Gateways {...props} />}
        {activeTab === "results" && <Results {...props} />}
        {activeTab === "settings" && <ExecutionSettings {...props} />}
      </main>
      <MobileNavigation activeTab={activeTab} setActiveTab={setActiveTab} />
      {toast && (
        <div className={`toast ${toast.tone}`} role="status">
          {toast.tone === "error" ? <XCircle size={18} /> : <CheckCircle2 size={18} />}
          {toast.message}
        </div>
      )}
    </div>
  );
}

function Brand() {
  return (
    <div className="brand">
      <span className="brand-mark"><Bot size={23} /></span>
      <span><strong>Ghost</strong><small>CLICKER</small></span>
    </div>
  );
}

function Sidebar({ activeTab, setActiveTab, database }) {
  return (
    <aside className="sidebar">
      <Brand />
      <nav aria-label="Navigation principale">
        <p className="nav-label">ESPACE DE TRAVAIL</p>
        {navigation.map(({ id, label, icon: Icon }) => (
          <button key={id} className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>
            <Icon size={19} /> <span>{label}</span>
            {activeTab === id && <span className="nav-dot" />}
          </button>
        ))}
      </nav>
      <div className="sidebar-status">
        <span className={`status-orb ${database.configured && !database.last_error ? "online" : "local"}`} />
        <div><strong>{database.configured ? "Supabase" : "Stockage local"}</strong><small>{database.configured && !database.last_error ? "Synchronisé" : "Opérationnel"}</small></div>
      </div>
    </aside>
  );
}

function MobileHeader({ activeTab }) {
  const current = navigation.find((item) => item.id === activeTab);
  return (
    <header className="mobile-header">
      <Brand />
      <span className="mobile-page">{current?.label}</span>
    </header>
  );
}

function MobileNavigation({ activeTab, setActiveTab }) {
  return (
    <nav className="mobile-nav" aria-label="Navigation mobile">
      {navigation.map(({ id, label, icon: Icon }) => (
        <button key={id} className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>
          <Icon size={20} /> <span>{label}</span>
        </button>
      ))}
    </nav>
  );
}

function PageHeading({ eyebrow, title, description, action }) {
  return (
    <header className="page-heading">
      <div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>
      {action}
    </header>
  );
}

function Dashboard({ run, targets, reports, settings, pending, mutate, refreshStatus, goTo }) {
  const results = useMemo(() => reportResults(reports), [reports]);
  const successfulClicks = Number(settings.lifetime_successful_visits) || 0;
  const balance = Number.isFinite(Number(settings.earnings_balance))
    ? Number(settings.earnings_balance)
    : moneyForClicks(successfulClicks);
  const milestone = nextMilestone(successfulClicks);
  const recent = results.slice(0, 5);
  const failures = results.filter((item) => !item.success).length;
  const visibleSuccess = results.length ? Math.round(((results.length - failures) / results.length) * 100) : 0;
  const selectedTargets = targets.filter((target) => target.selected !== false).length;
  const engineActive = Boolean(run.running || run.interval_campaign_active);

  return (
    <div className="page dashboard-page">
      <PageHeading
        eyebrow="VUE D’ENSEMBLE"
        title="Bonjour, opérateur"
        description="Voici l’activité de votre moteur d’automatisation aujourd’hui."
        action={<button className="icon-button" onClick={() => refreshStatus(false)} aria-label="Actualiser"><RefreshCw size={18} /></button>}
      />

      <section className="dashboard-grid">
        <article className="balance-card">
          <div className="balance-topline"><span>SOLDE DISPONIBLE</span><WalletCards size={22} /></div>
          <div className="balance-value"><span>$</span>{balance.toFixed(2)}</div>
          <div className="balance-meta">
            <div><strong>{number(successfulClicks)}</strong><span>clics réussis au total</span></div>
            <span className="balance-rule">250 clics = {milestone.reward}$</span>
          </div>
          <div className="progress-track"><span style={{ width: `${milestone.progress}%` }} /></div>
          <div className="progress-copy"><span>{number(milestone.remaining)} clics avant le prochain palier</span><strong>+${milestone.reward}</strong></div>
          <div className="card-orbits" aria-hidden="true" />
        </article>

        <article className="run-card panel">
          <div className="panel-title-row">
            <div><span className="eyebrow">MOTEUR</span><h2>Exécution</h2></div>
            <span className={`run-pill ${engineActive ? "running" : "idle"}`}><span />{run.running ? "En cours" : run.interval_campaign_active ? "Planifiée" : "En veille"}</span>
          </div>
          <div className="run-core">
            <span className={`run-icon ${engineActive ? "pulse" : ""}`}>{engineActive ? <Activity size={28} /> : <Zap size={28} />}</span>
            <div><strong>{run.running ? `${number(run.completed_visits)} visites traitées` : run.interval_campaign_active ? `${number(run.interval_campaign_launched)} / ${number(run.interval_campaign_total)} exécutions lancées` : "Prêt à démarrer"}</strong><p>{engineActive ? run.next_run_at ? `Prochain lancement ${dateTime(run.next_run_at)}` : `Mode ${run.execution_mode || "fixe"}` : `${selectedTargets} URL${selectedTargets > 1 ? "s" : ""} sélectionnée${selectedTargets > 1 ? "s" : ""}`}</p></div>
          </div>
          <div className="run-actions">
            {engineActive ? (
              <button className="danger-button" disabled={pending} onClick={() => mutate("/api/stop", { method: "POST" })}><Square size={16} /> Arrêter</button>
            ) : (
              <button className="primary-button" disabled={pending || !selectedTargets} onClick={() => mutate("/api/run", { method: "POST" })}><Play size={16} fill="currentColor" /> Lancer maintenant</button>
            )}
            <button className="secondary-button" onClick={() => goTo("settings")}>Configurer <ChevronRight size={16} /></button>
          </div>
        </article>
      </section>

      <section className="metrics-grid" aria-label="Indicateurs récents">
        <Metric icon={CheckCircle2} label="Taux de réussite" value={`${visibleSuccess}%`} detail={`${results.length} résultats récents`} tone="green" />
        <Metric icon={Radio} label="URLs actives" value={selectedTargets} detail={`${targets.length} configurées`} tone="violet" />
        <Metric icon={OctagonX} label="Échecs récents" value={failures} detail="Sur les résultats conservés" tone="red" />
      </section>

      <section className="content-grid">
        <article className="panel activity-panel">
          <div className="panel-title-row"><div><span className="eyebrow">ACTIVITÉ</span><h2>Dernières visites</h2></div><button className="text-button" onClick={() => goTo("results")}>Tout voir <ChevronRight size={15} /></button></div>
          <VisitList results={recent} empty="Les visites apparaîtront ici après la première exécution." />
        </article>
        <article className="panel earnings-panel">
          <div className="panel-title-row"><div><span className="eyebrow">RÉMUNÉRATION</span><h2>Barème</h2></div><CircleDollarSign size={21} /></div>
          <div className="tier"><span className="tier-number">01</span><div><strong>Premiers 500 clics</strong><p>Deux blocs complets maximum</p></div><b>$12<small>/ 250</small></b></div>
          <div className="tier"><span className="tier-number">02</span><div><strong>À partir de 501 clics</strong><p>Chaque bloc complet suivant</p></div><b>$8<small>/ 250</small></b></div>
          <p className="fine-print"><ShieldCheck size={15} /> Un clic correspond à une visite réussie. Les échecs ne sont jamais comptés.</p>
        </article>
      </section>
    </div>
  );
}

function Metric({ icon: Icon, label, value, detail, tone }) {
  return (
    <article className="metric-card">
      <span className={`metric-icon ${tone}`}><Icon size={20} /></span>
      <div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div>
    </article>
  );
}

function VisitList({ results, empty }) {
  if (!results.length) return <EmptyState icon={ListChecks} text={empty} />;
  return (
    <div className="visit-list">
      {results.map((result, index) => (
        <div className="visit-item" key={`${result.run_id}-${result.visit_number}-${index}`}>
          <span className={`result-dot ${result.success ? "success" : "failure"}`}>{result.success ? <Check size={15} /> : <X size={15} />}</span>
          <div className="visit-main"><strong>{result.target_alias || result.initial_url || "Visite"}</strong><span>{result.success ? "Visite réussie" : result.error || "Échec de la visite"}</span></div>
          <div className="visit-time"><strong>{duration(result.duration_ms)}</strong><span>{dateTime(result.started_at)}</span></div>
        </div>
      ))}
    </div>
  );
}

function EmptyState({ icon: Icon, text }) {
  return <div className="empty-state"><Icon size={25} /><p>{text}</p></div>;
}

function Targets({ targets, pending, mutate }) {
  const emptyTarget = { alias: "", url: "", button_text: "", second_button_text: "", selected: true };
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(emptyTarget);

  const openNew = () => { setEditing("new"); setForm(emptyTarget); };
  const openEdit = (target) => { setEditing(target.id); setForm({ ...target }); };
  const close = () => setEditing(null);
  const submit = async (event) => {
    event.preventDefault();
    const result = editing === "new"
      ? await mutate("/api/targets", { method: "POST", body: form })
      : await mutate(`/api/targets/${editing}`, { method: "PATCH", body: form });
    if (result) close();
  };

  return (
    <div className="page">
      <PageHeading eyebrow="DESTINATIONS" title="URLs" description="Gérez les pages visitées et les boutons à déclencher." action={<button className="primary-button" onClick={openNew}><Plus size={17} /> Ajouter une URL</button>} />
      <section className="panel resource-panel">
        <div className="resource-head"><span>{targets.length} URL{targets.length > 1 ? "s" : ""}</span><div className="resource-tools"><small>Modifications possibles lorsque le moteur est arrêté.</small><button type="button" disabled={pending || !targets.length} onClick={() => mutate("/api/targets/selection", { method: "POST", body: { mode: "all" } })}>Tout sélectionner</button><button type="button" disabled={pending || !targets.length} onClick={() => mutate("/api/targets/selection", { method: "POST", body: { mode: "none" } })}>Tout désélectionner</button></div></div>
        {!targets.length ? <EmptyState icon={Globe2} text="Ajoutez votre première URL pour commencer." /> : (
          <div className="resource-list">
            {targets.map((target) => (
              <article className="resource-row" key={target.id}>
                <span className="resource-icon"><Globe2 size={20} /></span>
                <div className="resource-copy"><strong>{target.alias}</strong><a href={target.url} target="_blank" rel="noreferrer">{target.url} <ExternalLink size={12} /></a><span>{target.button_text ? `Clic : ${target.button_text}${target.second_button_text ? ` → ${target.second_button_text}` : ""}` : "Redirection automatique, sans clic"}</span></div>
                <label className="switch"><input type="checkbox" checked={target.selected !== false} onChange={() => mutate(`/api/targets/${target.id}`, { method: "PATCH", body: { selected: target.selected === false } })} /><span /></label>
                <button className="icon-button small" onClick={() => openEdit(target)} aria-label={`Modifier ${target.alias}`}><Pencil size={16} /></button>
                <button className="icon-button small danger" onClick={() => window.confirm(`Supprimer « ${target.alias} » ?`) && mutate(`/api/targets/${target.id}`, { method: "DELETE" })} aria-label={`Supprimer ${target.alias}`}><Trash2 size={16} /></button>
              </article>
            ))}
          </div>
        )}
      </section>
      {editing && (
        <Modal title={editing === "new" ? "Nouvelle URL" : "Modifier l’URL"} close={close}>
          <form className="form-grid" onSubmit={submit}>
            <Field label="Alias" value={form.alias} onChange={(alias) => setForm({ ...form, alias })} placeholder="Page principale" required />
            <Field label="URL ou domaine" value={form.url} onChange={(url) => setForm({ ...form, url })} placeholder="getallink.com/butter" inputMode="url" autoCapitalize="none" required />
            <Field label="Premier bouton (optionnel)" value={form.button_text || ""} onChange={(button_text) => setForm({ ...form, button_text })} placeholder="Continuer" />
            <Field label="Second bouton (optionnel)" value={form.second_button_text || ""} onChange={(second_button_text) => setForm({ ...form, second_button_text })} placeholder="Confirmer" />
            <label className="check-row"><input type="checkbox" checked={form.selected !== false} onChange={(event) => setForm({ ...form, selected: event.target.checked })} /> Sélectionner pour les prochaines exécutions</label>
            <div className="modal-actions"><button type="button" className="secondary-button" onClick={close}>Annuler</button><button className="primary-button" disabled={pending}><Save size={16} /> Enregistrer</button></div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function Gateways({ gateways, pending, mutate }) {
  const [editing, setEditing] = useState(null);
  const [alias, setAlias] = useState("");
  const [gateway, setGateway] = useState("");
  const [active, setActive] = useState(false);

  const openNew = () => { setEditing("new"); setAlias(""); setGateway(""); setActive(!gateways.length); };
  const openEdit = (item) => { setEditing(item.id); setAlias(item.alias); setGateway(""); setActive(item.active); };
  const close = () => setEditing(null);
  const submit = async (event) => {
    event.preventDefault();
    const body = editing === "new" ? { alias, gateway, active } : { alias, ...(gateway ? { gateway } : {}) };
    const result = editing === "new"
      ? await mutate("/api/gateways", { method: "POST", body })
      : await mutate(`/api/gateways/${editing}`, { method: "PATCH", body });
    if (result) close();
  };

  return (
    <div className="page">
      <PageHeading eyebrow="CONNEXION" title="Réseau" description="Pilotez vos passerelles Rayobyte sans jamais afficher leurs identifiants." action={<button className="primary-button" onClick={openNew}><Plus size={17} /> Ajouter une passerelle</button>} />
      <section className="privacy-banner"><ShieldCheck size={21} /><div><strong>Identifiants protégés</strong><p>Les secrets sont conservés dans Supabase Vault et ne sont jamais renvoyés au navigateur.</p></div></section>
      <section className="panel resource-panel">
        {!gateways.length ? <EmptyState icon={ServerCog} text="Ajoutez une passerelle pour permettre les visites via Rayobyte." /> : (
          <div className="resource-list">
            {gateways.map((item) => (
              <article className="resource-row gateway-row" key={item.id}>
                <span className={`resource-icon ${item.active ? "active" : ""}`}><ServerCog size={20} /></span>
                <div className="resource-copy"><strong>{item.alias}</strong><span>{item.configured ? "Identifiants enregistrés" : "Identifiants à ressaisir"}</span></div>
                <span className={`run-pill ${item.active ? "running" : "idle"}`}><span />{item.active ? "Active" : "En veille"}</span>
                {!item.active && <button className="secondary-button compact" onClick={() => mutate(`/api/gateways/${item.id}/select`, { method: "POST" })}>Activer</button>}
                <button className="icon-button small" onClick={() => openEdit(item)} aria-label={`Modifier ${item.alias}`}><Pencil size={16} /></button>
                <button className="icon-button small danger" onClick={() => window.confirm(`Supprimer « ${item.alias} » ?`) && mutate(`/api/gateways/${item.id}`, { method: "DELETE" })} aria-label={`Supprimer ${item.alias}`}><Trash2 size={16} /></button>
              </article>
            ))}
          </div>
        )}
      </section>
      {editing && (
        <Modal title={editing === "new" ? "Nouvelle passerelle" : "Modifier la passerelle"} close={close}>
          <form className="form-grid" onSubmit={submit}>
            <Field label="Alias" value={alias} onChange={setAlias} placeholder="Rayobyte France" required />
            <Field label={editing === "new" ? "Passerelle" : "Nouveaux identifiants (optionnel)"} value={gateway} onChange={setGateway} placeholder="nom:mot-de-passe@hôte:port" type="password" required={editing === "new"} autoComplete="new-password" />
            <p className="field-help">Formats pris en charge : <code>utilisateur:mot-de-passe-country-CC@hôte:port</code> ou URL proxy. Aucun secret n’est affiché après sauvegarde.</p>
            {editing === "new" && <label className="check-row"><input type="checkbox" checked={active} onChange={(event) => setActive(event.target.checked)} /> Définir comme passerelle active</label>}
            <div className="modal-actions"><button type="button" className="secondary-button" onClick={close}>Annuler</button><button className="primary-button" disabled={pending}><Save size={16} /> Enregistrer</button></div>
          </form>
        </Modal>
      )}
    </div>
  );
}

function Results({ reports, pending, mutate, setReports, refreshStatus }) {
  const [filter, setFilter] = useState("all");
  const [selectedVisits, setSelectedVisits] = useState([]);
  const allResults = useMemo(() => reportResults(reports), [reports]);
  const results = allResults.filter((item) => filter === "all" || (filter === "success" ? item.success : !item.success));
  const success = allResults.filter((item) => item.success).length;
  const emptyRuns = reports.filter((report) => !(report.results || []).length);
  const visitKey = (result) => `${result.run_id}:${result.visit_number}`;

  const deleteSelectedVisits = async () => {
    const visits = allResults
      .filter((result) => selectedVisits.includes(visitKey(result)))
      .map((result) => ({ run_id: result.run_id, visit_number: result.visit_number }));
    const result = await mutate("/api/results/delete", { method: "POST", body: { action: "selected", visits, run_ids: [] } });
    if (result) { setSelectedVisits([]); setReports(result.reports || []); }
  };

  const deleteRun = async (runId) => {
    if (!window.confirm("Supprimer cette exécution et toutes ses visites ?")) return;
    const result = await mutate("/api/results/delete", { method: "POST", body: { action: "selected", visits: [], run_ids: [runId] } });
    if (result) setReports(result.reports || []);
  };

  const clearAll = async () => {
    if (!window.confirm("Effacer tout l’historique des résultats ?")) return;
    const result = await mutate("/api/results/delete", { method: "POST", body: { action: "all" } });
    if (result) { setSelectedVisits([]); setReports(result.reports || []); }
  };

  return (
    <div className="page">
      <PageHeading eyebrow="HISTORIQUE" title="Résultats" description="Consultez les visites conservées, exportez-les ou supprimez les éléments inutiles." action={<a className="primary-button" href="/results.csv"><ArrowDownToLine size={17} /> Exporter CSV</a>} />
      <section className="metrics-grid compact-metrics">
        <Metric icon={ListChecks} label="Visites conservées" value={allResults.length} detail={`${reports.length} exécutions`} tone="violet" />
        <Metric icon={CheckCircle2} label="Réussites" value={success} detail={allResults.length ? `${Math.round((success / allResults.length) * 100)} % du total` : "Aucune donnée"} tone="green" />
        <Metric icon={XCircle} label="Échecs" value={allResults.length - success} detail="À examiner" tone="red" />
      </section>
      <section className="panel result-panel">
        <div className="result-toolbar">
          <div className="segmented" role="group" aria-label="Filtrer les résultats">
            {[["all", "Tous"], ["success", "Réussis"], ["failure", "Échecs"]].map(([id, label]) => <button key={id} className={filter === id ? "active" : ""} onClick={() => setFilter(id)}>{label}</button>)}
          </div>
          <div className="toolbar-actions">
            <button className="icon-button small" onClick={() => refreshStatus(false)} aria-label="Actualiser"><RefreshCw size={16} /></button>
            <button className="danger-button compact" disabled={!selectedVisits.length || pending} onClick={deleteSelectedVisits}><Trash2 size={15} /> Visites ({selectedVisits.length})</button>
            <button className="danger-button compact ghost-danger" disabled={!allResults.length || pending} onClick={clearAll}>Tout effacer</button>
          </div>
        </div>
        {emptyRuns.length > 0 && <div className="empty-runs"><span>{emptyRuns.length} exécution{emptyRuns.length > 1 ? "s" : ""} sans visite</span>{emptyRuns.map((report) => <button key={report.run_id} onClick={() => deleteRun(report.run_id)}><Trash2 size={12} /> {dateTime(report.started_at)}</button>)}</div>}
        {!results.length ? <EmptyState icon={BarChart3} text="Aucun résultat ne correspond à ce filtre." /> : (
          <div className="table-scroll"><table><thead><tr><th><span className="sr-only">Sélection</span></th><th>État</th><th>Cible</th><th>Date</th><th>Durée</th><th>Destination</th><th><span className="sr-only">Actions</span></th></tr></thead><tbody>
            {results.map((result, index) => {
              const key = visitKey(result);
              const checked = selectedVisits.includes(key);
              return <tr key={`${key}-${index}`}><td><input type="checkbox" checked={checked} onChange={() => setSelectedVisits((current) => checked ? current.filter((item) => item !== key) : [...current, key])} aria-label={`Sélectionner la visite ${result.visit_number}`} /></td><td><span className={`result-badge ${result.success ? "success" : "failure"}`}>{result.success ? <Check size={13} /> : <X size={13} />}{result.success ? "Réussie" : "Échec"}</span></td><td><strong>{result.target_alias || "—"}</strong><small>{result.initial_url || ""}</small></td><td>{dateTime(result.started_at)}</td><td>{duration(result.duration_ms)}</td><td className="destination-cell">{result.success ? result.final_url || "—" : result.error || "—"}</td><td><button className="icon-button small danger" onClick={() => deleteRun(result.run_id)} aria-label="Supprimer toute cette exécution"><Trash2 size={14} /></button></td></tr>;
            })}
          </tbody></table></div>
        )}
      </section>
    </div>
  );
}

function ExecutionSettings({ settings, run, pending, mutate, setSettingsState }) {
  const [form, setForm] = useState(settings);
  const engineBusy = Boolean(run.running || run.interval_campaign_active);
  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const submit = async (event) => {
    event.preventDefault();
    const result = await mutate("/api/settings", { method: "POST", body: form });
    if (result?.settings) {
      setSettingsState(result.settings);
      setForm(result.settings);
    }
  };

  return (
    <div className="page">
      <PageHeading eyebrow="AUTOMATISATION" title="Réglages" description="Ajustez le rythme et le mode d’exécution du moteur." />
      <form className="settings-layout" onSubmit={submit}>
        <section className="panel settings-panel">
          <div className="setting-heading"><span className="metric-icon violet"><Gauge size={20} /></span><div><h2>Mode d’exécution</h2><p>Détermine le nombre de passages sur les URLs sélectionnées.</p></div></div>
          <div className="mode-grid">
            {[{ id: "fixed", title: "Fixe", text: "Nombre exact de répétitions", icon: ListChecks }, { id: "range", title: "Intervalle", text: "Campagne répartie sur plusieurs jours", icon: RotateCw }, { id: "continuous", title: "Continu", text: "Cycles successifs jusqu’à l’arrêt", icon: TrendingUp }].map(({ id, title, text, icon: Icon }) => (
              <label className={`mode-option ${form.execution_mode === id ? "selected" : ""}`} key={id}><input type="radio" name="execution_mode" value={id} checked={form.execution_mode === id} onChange={() => setForm((current) => ({ ...current, execution_mode: id, ...(id === "range" ? { schedule_enabled: false } : {}) }))} /><Icon size={20} /><strong>{title}</strong><span>{text}</span></label>
            ))}
          </div>
          <div className={form.execution_mode === "range" ? "campaign-fields" : "input-row"}>
            {form.execution_mode === "fixed" && <NumberField label="Répétitions" value={form.repetition_count} onChange={(value) => update("repetition_count", value)} min="1" max="1000" />}
            {form.execution_mode === "continuous" && <><NumberField label="Minimum par cycle" value={form.repetition_min} onChange={(value) => update("repetition_min", value)} min="1" max="1000" /><NumberField label="Maximum par cycle" value={form.repetition_max} onChange={(value) => update("repetition_max", value)} min="1" max="1000" /></>}
            {form.execution_mode === "range" && <><NumberField label="Nombre d’exécutions souhaité" value={form.interval_execution_count} onChange={(value) => update("interval_execution_count", value)} min="1" max="1000" /><NumberField label="Durée (jours)" value={form.interval_duration_days} onChange={(value) => update("interval_duration_days", value)} min="1" max="3650" /><NumberField label="Répétitions par exécution" value={form.interval_repetition_count} onChange={(value) => update("interval_repetition_count", value)} min="1" max="1000" /></>}
          </div>
        </section>

        <section className="panel settings-panel">
          <div className="setting-heading"><span className="metric-icon green"><Clock3 size={20} /></span><div><h2>Planification récurrente</h2><p>{form.execution_mode === "range" ? "Le mode Intervalle crée déjà son propre calendrier aléatoire." : "Relance automatiquement le mode choisi à cadence régulière."}</p></div><label className="switch push-right"><input type="checkbox" disabled={form.execution_mode === "range"} checked={Boolean(form.schedule_enabled) && form.execution_mode !== "range"} onChange={(event) => update("schedule_enabled", event.target.checked)} /><span /></label></div>
          <div className={form.schedule_enabled && form.execution_mode !== "range" ? "schedule-fields single" : "schedule-fields single disabled"}>
            <NumberField label="Intervalle (minutes)" value={form.interval_minutes} onChange={(value) => update("interval_minutes", value)} min="60" max="10080" />
          </div>
        </section>
        <div className="settings-save"><span>{engineBusy ? "Arrêtez le moteur ou la campagne pour modifier la configuration." : "Les réglages seront appliqués aux prochaines exécutions."}</span><button className="primary-button" disabled={pending || engineBusy}><Save size={16} /> Enregistrer les réglages</button></div>
      </form>
    </div>
  );
}

function Modal({ title, close, children }) {
  useEffect(() => {
    const escape = (event) => event.key === "Escape" && close();
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [close]);
  return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}><section className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div className="modal-head"><h2 id="modal-title">{title}</h2><button className="icon-button small" onClick={close} aria-label="Fermer"><X size={18} /></button></div>{children}</section></div>;
}

function Field({ label, value, onChange, type = "text", ...props }) {
  return <label className="field"><span>{label}</span><input type={type} value={value} onChange={(event) => onChange(event.target.value)} {...props} /></label>;
}

function NumberField({ label, value, onChange, ...props }) {
  return <label className="field"><span>{label}</span><input type="number" value={value ?? ""} onChange={(event) => onChange(event.target.value)} {...props} /></label>;
}

createRoot(document.getElementById("root")).render(<App />);
