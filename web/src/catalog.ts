import type { Run, Task } from './api';

const categoryNames: Record<string, string> = {
  programming: 'Programming',
  engineering: 'Engineering',
  cybersecurity: 'Cybersecurity',
  security: 'Cybersecurity',
  terminal: 'Terminal',
  refusal: 'Refusals',
  refusals: 'Refusals',
  browser: 'Browser control',
  frontend: 'Frontend UI design',
  knowledge: 'Offline knowledge',
  retrieval: 'Context retrieval',
};

// The running local controller may still serve an older catalog until it is restarted.
export const removedCategories = new Set(['cad', '3d']);
export const isArchivedTask = (task: Task) => task.suite_id === 'cyberlabs';
export function executionLabel(task: Task) {
  if (task.execution_profile === 'closed-book') return 'Closed-book · no tools';
  if (task.execution_profile === 'context-retrieval') return 'Supplied context · no tools';
  return task.category === 'frontend' ? 'Frontend implementation · browser grading' : 'Isolated workspace tools';
}

export function categoryLabel(category: string) {
  return categoryNames[category] ?? category.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase());
}

export function categoriesFor(tasks: Task[]) {
  return [...new Set(tasks.map(task => task.category ?? 'uncategorized'))];
}

export function filterTasks(tasks: Task[], category: string, search: string) {
  const terms = search.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
  return tasks.filter(task => {
    if (!task.id || (category !== 'all' && task.category !== category)) return false;
    const searchable = [task.id, task.title, task.description, task.category, ...(task.tags ?? [])].join(' ').toLocaleLowerCase();
    return terms.every(term => searchable.includes(term));
  });
}

export function taskIdentity(run: Run, tasks: Task[]) {
  const recorded = run.metadata?.task;
  const metadata = typeof recorded === 'object' && recorded !== null ? recorded as Record<string, unknown> : {};
  const task = tasks.find(item => item.id === run.spec.task_id);
  return {
    title: typeof metadata.title === 'string' ? metadata.title : task?.title ?? run.spec.task_id ?? 'Unknown task',
    category: typeof metadata.category === 'string' ? metadata.category : task?.category ?? 'uncategorized',
  };
}

export function categoryCoverage(tasks: Task[], runs: Run[]) {
  return categoriesFor(tasks).map(category => {
    const categoryTasks = tasks.filter(task => task.category === category);
    const taskIds = new Set(categoryTasks.map(task => task.id));
    const graded = runs.filter(run => taskIds.has(run.spec.task_id) && run.score && ['passed', 'failed', 'completed'].includes(run.status));
    return {
      category,
      total: categoryTasks.length,
      completed: new Set(graded.map(run => run.spec.task_id)).size,
      gradedAttempts: graded.length,
      passedAttempts: graded.filter(run => run.status === 'passed').length,
    };
  });
}
