"""Fail-closed region navigation, with evidenced same-county 면 → 읍 promotion."""
import re
from urllib.parse import parse_qs, urljoin, urlparse
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


class RegionNavigationError(RuntimeError):
    pass


_REGION_TOTAL_JS = r"""() => {
    const totals = [...new Set([...(document.body?.innerText || '').matchAll(/총\s*([\d,]+)\s*개\s*단지/g)]
        .map(match => Number(match[1].replaceAll(',', ''))))];
    if (totals.length > 1) return null;
    const cards = [...document.querySelectorAll('a[href^="/complexes/"]')]
        .filter(a => /^\/complexes\/\d+$/.test(a.getAttribute('href') || ''));
    const empty = [...document.querySelectorAll('p[class*="ComplexListByRegion"][class*="nodata"]')]
        .some(p => p.getClientRects().length && p.innerText.trim() === '단지가 없습니다.');
    if (!totals.length) return empty && !cards.length ? 0 : null;
    const total = totals[0];
    if (total === 0) return cards.length ? null : 0;
    if (empty) return null;
    return cards.some(a => a.closest('li')?.querySelector('strong[class*="name"]')?.textContent?.trim())
        ? total : null;
}"""


async def read_region_total(page, timeout=20000):
    """Require a loaded inventory or the site's explicit, visible empty state."""
    try:
        await page.wait_for_function(f'() => ({_REGION_TOTAL_JS})() !== null', timeout=timeout)
    except PlaywrightTimeoutError as exc:
        raise RegionNavigationError('region readiness is unproven') from exc
    total = await page.evaluate(_REGION_TOTAL_JS)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RegionNavigationError('region total changed before it could be read')
    return total


def _identity(url):
    parsed = urlparse(url)
    return parsed.scheme, parsed.netloc, parsed.path, parse_qs(parsed.query)


def _replacement(target_url, actual_url, name, links):
    expected = _identity(target_url)
    actual = _identity(actual_url)
    query = expected[3]
    # Only a missing leaf returning to its OWN parent can be repaired.
    if (expected[:3] != ('https', 'fin.land.naver.com', '/regions')
            or actual[:3] != expected[:3]
            or set(query) != {'si', 'gun', 'eup'}
            or any(len(v) != 1 for v in query.values())
            or actual[3] != {k: v for k, v in query.items() if k != 'eup'}
            or not name or not re.fullmatch(r'.+면', name)):
        return None
    candidates = set()
    for link in links:
        new_name = link['name'].strip()
        new_url = urljoin(actual_url, link['url'])
        candidate = _identity(new_url)
        if (candidate[:3] == expected[:3]
                and set(candidate[3]) == {'si', 'gun', 'eup'}
                and all(len(v) == 1 for v in candidate[3].values())
                and candidate[3]['si'] == query['si']
                and candidate[3]['gun'] == query['gun']
                and new_name in {name, name[:-1] + '읍'}):
            candidates.add((new_url, new_name))
    if len(candidates) == 1:
        url, new_name = candidates.pop()
        if new_name == name[:-1] + '읍' and _identity(url) != expected:
            return {'url': url, 'name': new_name}
    return None


async def navigate_region(page, target_url, region_name=None):
    response = await page.goto(target_url, wait_until='domcontentloaded', timeout=60000)
    if response is not None and response.status >= 400:
        raise RegionNavigationError(f'region HTTP {response.status}: {page.url}')
    if _identity(page.url) == _identity(target_url):
        return {'url': page.url, 'name': region_name}
    expected = _identity(target_url)
    parent = (*expected[:3], {k: v for k, v in expected[3].items() if k != 'eup'})
    if 'eup' in expected[3] and _identity(page.url) == parent:
        try:
            await page.wait_for_selector('a[href*="eup="]', timeout=10000)
        except PlaywrightTimeoutError as exc:
            raise RegionNavigationError(f'region redirect has no loaded leaf list: {target_url} -> {page.url}') from exc
    links = await page.evaluate("""() => [...document.querySelectorAll('a[href]')]
        .map(a => ({url:a.getAttribute('href'), name:a.textContent.trim()}))""")
    replacement = _replacement(target_url, page.url, region_name, links)
    if replacement is None:
        raise RegionNavigationError(f'region redirect: {target_url} -> {page.url}; no unique verified replacement')
    response = await page.goto(replacement['url'], wait_until='domcontentloaded', timeout=60000)
    if response is not None and response.status >= 400:
        raise RegionNavigationError(f'region HTTP {response.status}: {page.url}')
    if _identity(page.url) != _identity(replacement['url']):
        raise RegionNavigationError(f'region replacement redirect: {replacement["url"]} -> {page.url}')
    return replacement
