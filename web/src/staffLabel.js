// The staff selectors tell a stand from a chair: «Эльмира — администратор». The label is only
// display text — whatever needs the bare name (a section title) reads `option.dataset.name`.
const ROLE_KEYS = {
  administrator: 'dash.staffRoleAdministrator',
  barber: 'dash.staffRoleBarber',
  mixed: 'dash.staffRoleMixed',
};

export function staffRoleLabel(category, translate) {
  const key = ROLE_KEYS[category];
  return key ? translate(key) : '';
}

// `name` is what titles show (with the branch when the list spans several); `text` is the option text.
// A barber's level («ПРЕМИУМ-барбер») says more than the generic role word, so it takes its place;
// a mixed person keeps the administrator part next to it: «ПРЕМИУМ-барбер / администратор».
export function staffOption(staff, { branchTitle = '', translate }) {
  const base = String(staff.name ?? '');
  const name = branchTitle ? `${base} · ${branchTitle}` : base;
  const level = staff.level?.title;
  const role = level
    ? (staff.category === 'mixed' ? `${level} / ${staffRoleLabel('administrator', translate)}` : level)
    : staffRoleLabel(staff.category, translate);
  const text = role ? `${base} — ${role}${branchTitle ? ` · ${branchTitle}` : ''}` : name;
  return { name, text };
}
