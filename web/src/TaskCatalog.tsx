import type { Run, Task } from './api';
import { categoriesFor, categoryCoverage, categoryLabel } from './catalog';

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

export default function TaskCatalog({ tasks, filtered, selected, category, search, onCategory, onSearch, onSelect }: {
  tasks: Task[]; filtered: Task[]; selected: Task | undefined; category: string; search: string;
  onCategory: (category: string) => void; onSearch: (search: string) => void; onSelect: (id: string) => void;
}) {
  return <section className="panel catalog" id="catalog" aria-labelledby="catalog-heading">
    <div className="panel-heading"><div><span className="eyebrow">BENCHMARK CATALOG</span><h2 id="catalog-heading">Choose the work</h2></div><span className="history-count mono">{filtered.length} / {tasks.length} tasks</span></div>
    <div className="catalog-controls">
      <div className="catalog-search"><label htmlFor="task-search">Search benchmarks</label><input id="task-search" type="search" value={search} onChange={event => onSearch(event.target.value)} placeholder="Search tasks, capabilities, or tags" /></div>
      <div><label htmlFor="task-category">Category</label><select id="task-category" value={category} onChange={event => onCategory(event.target.value)}><option value="all">All categories</option>{categoriesFor(tasks).map(item => <option key={item} value={item}>{categoryLabel(item)}</option>)}</select></div>
      {(search || category !== 'all') && <button type="button" className="button button-quiet catalog-clear" onClick={() => { onSearch(''); onCategory('all'); }}>Clear filters</button>}
    </div>
    <div className="catalog-body">
      <div className="catalog-list" aria-label="Available benchmarks">
        {filtered.map(task => <button key={task.id} type="button" className={`catalog-task ${task.id === selected?.id ? 'selected' : ''}`} aria-label={`Select task ${task.title ?? task.id}`} aria-pressed={task.id === selected?.id} onClick={() => onSelect(task.id!)}>
          <span className="catalog-task-top"><strong>{task.title ?? task.id}</strong><span className={`difficulty difficulty-${task.difficulty ?? 'easy'}`}>{task.difficulty ?? 'easy'}</span></span>
          <span className="catalog-task-bottom"><span>{categoryLabel(task.category ?? 'uncategorized')}</span><span>{task.check_count ? `${task.check_count} checks` : 'Checks not listed'}</span></span>
        </button>)}
        {!filtered.length && <div className="catalog-empty"><h3>{tasks.length ? 'No matching benchmarks' : 'No benchmarks loaded'}</h3><p>{tasks.length ? 'Try a different category or search term.' : 'Refresh the workspace to load its task catalog.'}</p></div>}
      </div>
      <div className="catalog-detail" aria-live="polite">
        {selected ? <>
          <div className="catalog-detail-heading"><span className="tag">{categoryLabel(selected.category ?? 'uncategorized')}</span><span className={`difficulty difficulty-${selected.difficulty ?? 'easy'}`}>{selected.difficulty ?? 'easy'}</span></div>
          <h3>{selected.title ?? selected.id}</h3>
          <span className="catalog-task-id mono">{selected.id} · v{selected.version ?? '1.0.0'}</span>
          <div className="catalog-description">{selected.description}</div>
          <dl className="task-facts"><div><dt>Submission</dt><dd className="mono">{selected.submission_file ?? 'solution.py'}</dd></div><div><dt>Format</dt><dd>{selected.kind === 'json' ? 'Structured JSON' : 'Python implementation'}</dd></div><div><dt>Checks</dt><dd>{selected.check_count ?? 'Not listed'}</dd></div></dl>
          <div className="task-rubric"><span className="eyebrow">GRADING RUBRIC</span><p>{selected.rubric || 'The task’s independent grader checks the submitted artifact against its expected outcomes.'}</p></div>
          {!!selected.tags?.length && <div className="task-tags" aria-label="Task tags">{selected.tags.map(tag => <span key={tag}>{tag}</span>)}</div>}
          <a className="catalog-configure" href="#new-run">Configure this benchmark <span aria-hidden="true">↓</span></a>
        </> : <div className="catalog-empty"><h3>Select a benchmark</h3><p>Its instructions, submission format, and grading rubric will appear here.</p></div>}
      </div>
    </div>
  </section>;
}
