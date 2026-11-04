from html.parser import HTMLParser
import json
from pathlib import Path
import unittest
import xml.etree.ElementTree as ET

LANDING = Path(__file__).resolve().parents[1] / 'deploy/vps/landing'


class Metadata(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.canonicals = []
        self.meta = {}
        self.schemas = []
        self.in_schema = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonicals.append(attrs['href'])
        if tag == 'meta':
            self.meta[attrs.get('name', attrs.get('property'))] = attrs.get('content')
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.in_schema = True

    def handle_endtag(self, tag):
        if tag == 'script':
            self.in_schema = False

    def handle_data(self, data):
        if self.in_schema and data.strip():
            self.schemas.append(json.loads(data))


class PublicSEOTests(unittest.TestCase):
    def test_sitemap_matches_unique_public_canonicals(self):
        urls = [e.text for e in ET.parse(LANDING / 'sitemap.xml').findall('.//{*}loc')]
        expected = []
        for filename, route in [('index.html', '/'), ('app.html', '/app'), ('privacy.html', '/privacy')]:
            metadata = Metadata((LANDING / filename).read_text(encoding='utf-8'))
            url = 'https://www.cooperativems.com' + route
            self.assertEqual(metadata.canonicals, [url])
            self.assertNotIn('noindex', metadata.meta.get('robots', ''))
            self.assertTrue(metadata.meta['description'])
            expected.append(url)
        self.assertCountEqual(urls, expected)
        self.assertIn('https://www.cooperativems.com/sitemap.xml', (LANDING / 'robots.txt').read_text())

    def test_structured_data_and_share_image_are_valid(self):
        metadata = Metadata((LANDING / 'index.html').read_text(encoding='utf-8'))
        graph = metadata.schemas[0]['@graph']
        self.assertEqual({node['@type'] for node in graph}, {'Organization', 'WebSite', 'SoftwareApplication'})
        self.assertNotIn('aggregateRating', json.dumps(graph))
        image = metadata.meta['og:image'].removeprefix('https://www.cooperativems.com/')
        self.assertTrue((LANDING / image).is_file())

    def test_signin_is_noindex_and_app_guide_has_no_client_examples(self):
        self.assertIn('noindex', Metadata((LANDING / 'sign-in.html').read_text()).meta['robots'])
        guide = (LANDING / 'app.html').read_text(encoding='utf-8')
        self.assertNotIn('ooucoop', guide)
        self.assertNotIn('smtcoop', guide)
        self.assertIn('href="/sign-in"', guide)


if __name__ == '__main__':
    unittest.main()
