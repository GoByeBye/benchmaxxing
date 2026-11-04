import { useEffect, useState } from 'react';
import type { Run, Task } from './api';
import { categoriesFor, categoryCoverage, categoryLabel, executionLabel, isArchivedTask } from './catalog';

export function Coverage({ tasks, runs, category, onCategory }: {
  tasks: Task[]; runs: Run[]; category: string; onCategory: (category: string) => void;
}) {
  return <section className="coverage-section" aria-labelledby="coverage-heading">
    <div className="coverage-heading"><div><span className="eyebrow">COVERAGE, BY CATEGORY</span><h2 id="coverage-heading">Explore the benchmark library</h2></div><p>Across {runs.length >= 1000 ? 'the latest 1,000 runs' : 'loaded run history'}. Categories keep their own results.</p></div>
    <div className="coverage-grid">
      {categoryCoverage(tasks, runs).map(item => <button key={item.category} className={`coverage-card ${category === item.category ? 'selected' : ''}`} data-category={item.category} aria-label={`Filter ${categoryLabel(item.category)} benchmarks`} aria-pressed={category === item.category} onClick={() => onCategory(item.category)}>
        <div><strong>{categoryLabel(item.category)}</strong><span className="mono">{item.total} {item.total === 1 ? 'task' : 'tasks'}</span></div>
        <p><b>{item.completed}</b><span> / {item.total} graded tasks</span></p>
        <progress value={item.completed} max={item.total} aria-label={`${categoryLabel(item.category)} graded task coverage`} />
        <small><span className="coverage-passed">{item.passedAttempts} passed</span><span> · {item.gradedAttempts} graded attempts</span></small>
      </button>)}
    </div>
    <p className="coverage-note">Coverage counts distinct tasks with a scorecard. Repeat attempts are counted separately; interrupted or ungraded runs do not increase coverage.</p>
  </section>;
}

export default function TaskCatalog({ tasks, filtered, selected, category, search, suite, onSuite, onCategory, onSearch, onSelect }: {
  tasks: Task[]; filtered: Task[]; selected: Task | undefined; category: string; search: string; suite: string;
  onSuite: (suite: string) => void;
  onCategory: (category: string) => void; onSearch: (search: string) => void; onSelect: (id: string) => void;
}) {
  const [page, setPage] = useState(0);
  const pageSize = 40;
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const currentPage = Math.min(page, pages - 1);
  const suites = [...new Set(tasks.map(task => task.suite_id).filter((value): value is string => Boolean(value)))].sort();
  useEffect(() => { setPage(0); }, [category, search, suite]);
  return <section className="panel catalog" id="catalog" aria-labelledby="catalog-heading">
    <div className="panel-heading"><div><span className="eyebrow">BENCHMARK CATALOG</span><h2 id="catalog-heading">Choose the work</h2></div><span className="history-count mono">{filtered.length} / {tasks.length} tasks</span></div>
    <div className="catalog-controls">
      <div className="catalog-search"><label htmlFor="task-search">Search benchmarks</label><input id="task-search" type="search" value={search} onChange={event => onSearch(event.target.value)} placeholder="Search tasks, capabilities, or tags" /></div>
      <div><label htmlFor="task-category">Category</label><select id="task-category" value={category} onChange={event => onCategory(event.target.value)}><option value="all">All categories</option>{categoriesFor(tasks).map(item => <option key={item} value={item}>{categoryLabel(item)}</option>)}</select></div>
      <div><label htmlFor="task-suite">Task suite</label><select id="task-suite" value={suite} onChange={event => onSuite(event.target.value)}><option value="all">All installed tasks</option><option value="original">Original Benchmaxxing packs</option>{suites.map(item => <option key={item} value={item}>{item}</option>)}</select></div>
      {(search || category !== 'all' || suite !== 'all') && <button type="button" className="button button-quiet catalog-clear" onClick={() => { onSearch(''); onCategory('all'); onSuite('all'); }}>Clear filters</button>}
    </div>
    <div className="catalog-body">
      <div className="catalog-list" aria-label="Available benchmarks">
        {filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize).map(task => <button key={task.id} type="button" className={`catalog-task ${task.id === selected?.id ? 'selected' : ''}`} aria-label={`Select task ${task.title ?? task.id}`} aria-pressed={task.id === selected?.id} onClick={() => onSelect(task.id!)}>
          <span className="catalog-task-top"><strong>{task.title ?? task.id}</strong><span className={`difficulty difficulty-${task.difficulty ?? 'easy'}`}>{task.difficulty ?? 'easy'}</span></span>
          <span className="catalog-task-bottom"><span>{categoryLabel(task.category ?? 'uncategorized')}{task.suite_id ? ` · ${task.suite_id}` : ''}</span><span>{task.kind === 'native' ? task.prepared ? 'Prepared' : 'Needs preparation' : task.check_count ? `${task.check_count} checks` : 'Checks not listed'}</span></span>
        </button>)}
        {!filtered.length && <div className="catalog-empty"><h3>{tasks.length ? 'No matching benchmarks' : 'No benchmarks loaded'}</h3><p>{tasks.length ? 'Try a different category or search term.' : 'Refresh the workspace to load its task catalog.'}</p></div>}
        {pages > 1 && <div className="catalog-pagination"><button type="button" className="button button-quiet" onClick={() => setPage(currentPage - 1)} disabled={!currentPage} aria-label="Previous task page">←</button><span>{currentPage * pageSize + 1}–{Math.min((currentPage + 1) * pageSize, filtered.length)} of {filtered.length}</span><button type="button" className="button button-quiet" onClick={() => setPage(currentPage + 1)} disabled={currentPage >= pages - 1} aria-label="Next task page">→</button></div>}
      </div>
      <div className="catalog-detail" aria-live="polite">
        {selected ? <>
          <div className="catalog-detail-heading"><span className="tag">{categoryLabel(selected.category ?? 'uncategorized')}</span><span className={`difficulty difficulty-${selected.difficulty ?? 'easy'}`}>{selected.difficulty ?? 'easy'}</span></div>
          <h3>{selected.title ?? selected.id}</h3>
          <span className="catalog-task-id mono">{selected.id} · v{selected.version ?? '1.0.0'}</span>
          {selected.suite_id && <div className="task-provenance"><strong>{selected.suite_id}</strong><span>{selected.upstream_id} · {selected.upstream_revision}</span><span>{selected.variant}</span><b className={selected.prepared ? 'task-prepared' : 'task-unprepared'}>{selected.prepared ? 'Environment prepared' : 'Prepare this suite before running'}</b></div>}
          {isArchivedTask(selected) && <p className="provider-blockers">Archived from active scope. Existing attempts and artifacts remain inspectable.</p>}
          <div className="catalog-description">{selected.description}</div>
          <dl className="task-facts"><div><dt>Submission</dt><dd className="mono">{selected.kind === 'answer' ? 'JSON response' : selected.submission_file ?? 'solution.py'}</dd></div><div><dt>Format</dt><dd>{selected.kind === 'native' ? selected.category === 'frontend' ? 'HTML / CSS / JavaScript' : 'Native benchmark workflow' : selected.kind === 'answer' ? 'Direct structured answer' : selected.kind === 'json' ? 'Structured JSON' : 'Python implementation'}</dd></div><div><dt>Checks</dt><dd>{selected.check_count ?? 'Native grader'}</dd></div><div><dt>Profile</dt><dd>{executionLabel(selected)}</dd></div></dl>
          <div className="task-rubric"><span className="eyebrow">GRADING RUBRIC</span><p>{selected.rubric || 'The task’s independent grader checks the submitted artifact against its expected outcomes.'}</p></div>
          {!!selected.tags?.length && <div className="task-tags" aria-label="Task tags">{selected.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
          <a className="catalog-configure" href="#new-run">Configure this benchmark <span aria-hidden="true">↓</span></a>
        </> : <div className="catalog-empty"><h3>Select a benchmark</h3><p>Its instructions, submission format, and grading rubric will appear here.</p></div>}
      </div>
    </div>
  </section>;
}
