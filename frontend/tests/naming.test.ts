import { describe, it, expect } from 'vitest';
import { validateTemplate, sampleTemplate, namingVariables } from '../src/config/namingVariables';
import defaults from '../src/config/naming-defaults.json';
describe('naming templates', () => {
  it('accepts every server default', () => {
    for (const [key, value] of Object.entries(defaults))
      expect(validateTemplate(key as keyof typeof defaults, value)).toBe('');
  });
  it('accepts numeric season formats and preserves escaped braces in the example', () => {
    expect(validateTemplate('tv_file_template', '{{media}}.S{season:02d}E{episode:03d}{ext}')).toBe(
      '',
    );
    expect(sampleTemplate('{{media}}.S{season:02d}E{episode:03d}{ext}')).toBe(
      '{media}.S01E002.mkv',
    );
  });
  it.each(['{missing}{ext}', '{title:02d}{ext}', '{title}/{ext}', '{title', 'literal{ext}}'])(
    'rejects invalid template %s',
    (value) => expect(validateTemplate('movie_file_template', value)).not.toBe(''),
  );
  it('enforces movie, season and video requirements', () => {
    expect(validateTemplate('movie_file_template', '{title}')).not.toBe('');
    expect(validateTemplate('movie_file_template', '{title}{season}{ext}')).not.toBe('');
    expect(validateTemplate('season_directory_template', '{title}')).not.toBe('');
  });
  it('provides a unique variable dictionary', () =>
    expect(new Set(namingVariables.map((v) => v.key)).size).toBe(namingVariables.length));
});
