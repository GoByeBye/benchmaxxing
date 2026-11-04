import { useMemo, useState } from 'react';
import { formatDuration } from './api';
import type { Run } from './api';
import { categoryLabel } from './catalog';
import { compareModels, modelCategoryOverview, modelKey } from './comparisonMath';
import './modelComparison.css';

export default function ModelComparison({ runs }: { runs: Run[] }) {
  const models = useMemo(() => {
    const values = new Map<string, string>();
    runs.forEach(run => {
      const key = modelKey(run);
      if (key) values.set(key, `${run.spec.inference!.model} · ${run.spec.inference!.base_url} · ${run.spec.inference!.mode ?? 'offline'} · ${run.spec.inference!.api_style === 'responses' ? 'Responses' : 'Chat Completions'}${run.spec.inference!.responses_reasoning ? ' · encrypted reasoning' : ''}`);
    });
    return [...values].sort((a, b) => a[1].localeCompare(b[1]));
  }, [runs]);
  const [leftChoice, setLeft] = useState('');
  const [rightChoice, setRight] = useState('');
  const left = models.some(([key]) => key === leftChoice) ? leftChoice : models[0]?.[0] ?? '';
  const right = models.some(([key]) => key === rightChoice && key !== left) ? rightChoice : models.find(([key]) => key !== left)?.[0] ?? '';
  const comparison = useMemo(() => compareModels(runs, left, right), [runs, left, right]);
  const overview = useMemo(() => modelCategoryOverview(runs), [runs]);
  const labels = new Map(models);
  return <section className="panel model-comparison" id="comparison" aria-labelledby="comparison-heading">
    <div className="panel-heading"><div><span className="eyebrow">MATCHED EXPERIMENTS</span><h2 id="comparison-heading">Compare model results</h2></div></div>
    <p className="comparison-explanation">Category pass rates use only matching task IDs, versions, hashes, harnesses, graders, generation settings, budgets, API styles, vLLM priorities, recorded concurrency limits, images and network conditions. Scripted references are excluded. Results describe the loaded history, not an overall model ranking.</p>
    {overview.length > 0 && <div className="comparison-overview" aria-label="Category scores by model">
      <h3>Category scores by model</h3>
      <p>Each bar uses that model's graded attempts in the category. Task coverage is shown beside it; bars with different task sets are descriptive, not a head-to-head result.</p>
      {overview.map(row => <div className="comparison-overview-category" key={row.category}>
        <h4>{categoryLabel(row.category)}</h4>
        {row.models.map(model => {
          const percent = Math.round(100 * model.passed / model.graded);
          return <div className="comparison-overview-row" key={model.key}>
            <span title={labels.get(model.key)}>{labels.get(model.key) ?? model.key}</span>
            <div className="comparison-overview-track" aria-hidden="true"><span style={{ width: `${percent}%` }} /></div>
            <strong>{percent}%</strong><small>{model.passed}/{model.graded} passed · {model.tasks} {model.tasks === 1 ? 'task' : 'tasks'}{model.medianSeconds === null ? '' : ` · median ${formatDuration(model.medianSeconds)} to pass (${model.timedPassed})`}</small>
          </div>;
        })}
      </div>)}
    </div>}
    {models.length < 2 ? <p className="comparison-empty">Run two model profiles on the same tasks and settings to compare their results.</p> : <>
      <div className="comparison-selectors">{[left, right].map((value, index) => <label key={index}>Model {index === 0 ? 'A' : 'B'}<select aria-label={`Comparison model ${index === 0 ? 'A' : 'B'}`} value={value} onChange={event => (index === 0 ? setLeft : setRight)(event.target.value)}>{models.map(([key, label]) => <option key={key} value={key} disabled={index === 1 && key === left}>{label}</option>)}</select></label>)}</div>
      <p className="comparison-explanation">For each shared task and configuration, both profiles use the same number of latest graded attempts. Extra attempts are excluded. Coverage counts matching configurations among the union of configurations with sufficient provenance and grades. Network isolation for known harness versions is defined by that harness; inference access modes must also match.</p>
      {!comparison.rows.some(row => row.shared) && <p className="comparison-empty" role="status">No matching graded configurations yet. Check the task versions, settings and recorded provenance.</p>}
      <div className="comparison-categories">{comparison.rows.map(row => <article className="comparison-category" key={row.category}>
        <div className="comparison-category-heading"><h3>{categoryLabel(row.category)}</h3><span>Coverage: {row.shared} / {row.available} configurations</span></div>
        {row.attempts.map((attempt, index) => {
          const percent = attempt.count ? Math.round(100 * attempt.passed / attempt.count) : null;
          return <div className="comparison-result" key={index}><span>Model {index === 0 ? 'A' : 'B'}</span><div className={`comparison-track comparison-model-${index}`} aria-hidden="true"><span style={{ width: `${percent ?? 0}%` }} /></div><strong>{percent === null ? 'No matched grades' : `${percent}%`}</strong><small>{attempt.passed} passed / {attempt.count} graded attempts</small></div>;
        })}
        <div className="comparison-timing"><strong>Time to successful completion</strong><span>Median elapsed execution time; queued time and failed attempts are excluded.</span>{row.timings.map((timing, index) => {
          const maximum = Math.max(1, ...row.timings.map(item => item.medianSeconds ?? 0));
          return <div className="comparison-timing-row" key={index}><span>Model {index === 0 ? 'A' : 'B'}</span><div className="comparison-time-track" aria-hidden="true"><span style={{ width: `${timing.medianSeconds === null ? 0 : Math.max(2, 100 * timing.medianSeconds / maximum)}%` }} /></div><strong>{timing.medianSeconds === null ? 'Unknown' : formatDuration(timing.medianSeconds)}</strong><small>{timing.timedPassed} timed pass{timing.timedPassed === 1 ? '' : 'es'}</small></div>;
        })}</div>
      </article>)}</div>
      <div className="table-scroll"><table className="comparison-outcomes"><caption>Attempts excluded from the category bars</caption><thead><tr><th>Profile</th><th>Ungraded / needs review / errors</th><th>Cancelled</th><th>Active</th><th>Missing provenance</th><th>Unmatched / extra graded</th></tr></thead><tbody>{comparison.summary.map((item, index) => <tr key={index}><th>Model {index === 0 ? 'A' : 'B'}</th><td>{item.ungraded}</td><td>{item.cancelled}</td><td>{item.active}</td><td>{item.missing}</td><td>{item.unmatched}</td></tr>)}</tbody></table></div>
      <p className="comparison-explanation">Pass/fail grades are separate from partial or judge-only scores. Ungraded, cancelled and active attempts never count as failures. Small samples and different task mixes limit conclusions.</p>
    </>}
  </section>;
}
