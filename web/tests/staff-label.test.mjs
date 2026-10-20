import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import { staffOption, staffRoleLabel } from '../src/staffLabel.js';

const translate = (key) => ({
  'dash.staffRoleAdministrator': 'администратор',
  'dash.staffRoleBarber': 'барбер',
  'dash.staffRoleMixed': 'барбер/админ',
})[key];

test('a labelled row shows the role and keeps the bare name for titles', () => {
  assert.deepEqual(
    staffOption({ name: 'Эльмира', category: 'administrator' }, { translate }),
    { name: 'Эльмира', text: 'Эльмира — администратор' },
  );
  assert.equal(staffOption({ name: 'Ян', category: 'mixed' }, { translate }).text, 'Ян — барбер/админ');
});

test('the branch suffix follows the label and stays in the title name', () => {
  assert.deepEqual(
    staffOption({ name: 'Ян', category: 'barber' }, { branchTitle: 'Арбат', translate }),
    { name: 'Ян · Арбат', text: 'Ян — барбер · Арбат' },
  );
});

test('a barber level replaces the generic role word', () => {
  const level = { key: 'премиумбарбер', title: 'ПРЕМИУМ-барбер' };
  assert.deepEqual(
    staffOption({ name: 'Рауф', category: 'barber', level }, { branchTitle: 'Арбат', translate }),
    { name: 'Рауф · Арбат', text: 'Рауф — ПРЕМИУМ-барбер · Арбат' },
  );
  assert.equal(
    staffOption({ name: 'Рауф', category: 'mixed', level }, { branchTitle: 'Арбат', translate }).text,
    'Рауф — ПРЕМИУМ-барбер / администратор · Арбат',
  );
  assert.equal(
    staffOption({ name: 'Рауф', category: 'barber', level: null }, { translate }).text,
    'Рауф — барбер',
  );
});

test('a row without a category (portal owner) is shown as before', () => {
  assert.deepEqual(staffOption({ name: 'Анна', category: null }, { branchTitle: 'Арбат', translate }), {
    name: 'Анна · Арбат',
    text: 'Анна · Арбат',
  });
  assert.equal(staffRoleLabel(undefined, translate), '');
});

test('titles read the bare name, never the option text that carries the label', () => {
  const main = readFileSync(new URL('../src/main.js', import.meta.url), 'utf8');
  assert.match(main, /option\.dataset\.name = name/);
  assert.doesNotMatch(main, /staff\.selectedOptions\[0\]\??\.textContent/);
  assert.match(main, /selectedOption\.dataset\.name/);
});
