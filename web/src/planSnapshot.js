// Reshapes a clone of the plan/fact table into a fact-only picture: the plan, remaining and completion rows go,
// the group's name cell (rowspan over all of them) moves onto the fact row, and a heading that names the plan gets
// its fact wording.
export function keepFactRows(root) {
  root.querySelectorAll('tr[data-plan-row="plan"]').forEach((planRow) => {
    const nameCell = planRow.querySelector('.branch-cell');
    if (!nameCell) return;
    let factRow = planRow.nextElementSibling;
    while (factRow && factRow.dataset.planRow !== 'fact') {
      if (factRow.dataset.planRow === 'plan') return;
      factRow = factRow.nextElementSibling;
    }
    if (!factRow) return;
    nameCell.removeAttribute('rowspan');
    factRow.prepend(nameCell);
  });
  root.querySelectorAll('tr[data-plan-row]:not([data-plan-row="fact"])').forEach((row) => row.remove());
  root.querySelectorAll('[data-plan-col]:not([data-plan-col="fact"])').forEach((cell) => cell.remove());
  root.querySelectorAll('[data-fact-title]').forEach((heading) => {
    heading.textContent = heading.dataset.factTitle;
  });
}
