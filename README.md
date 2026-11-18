# check-site

**Audit SEO, GEO et technique d'un site entier, en une commande.**

> **In English:** a single-file, dependency-free Python crawler that audits a whole website for SEO, GEO (AI search) and technical issues, comparing pages with each other (duplicate titles, repeated FAQ questions, keyword cannibalisation…). Read-only, 43 checks, Markdown or JSON report. The report is in French. `python check_site.py https://www.example.com`

`check_site.py` parcourt toutes les pages d'un site et signale ce qui gêne son référencement, sur Google comme dans les moteurs IA. Sa particularité : il **compare les pages entre elles**. Il trouve donc aussi ce qu'un contrôle page par page ne voit pas, comme les titles en double, une même question de FAQ sur plusieurs pages ou deux pages qui visent le même mot-clé.

- Un seul fichier Python, **sans dépendance** (bibliothèque standard).
- **En lecture seule** : des requêtes GET, une à la fois, avec une pause entre chaque.
- Rapport en Markdown (lisible) ou en JSON (pour un autre outil).

## Installation

Python 3.9 ou plus récent (testé en 3.13). Rien d'autre :

```bash
git clone https://github.com/nicolas-datapet/check-site.git
cd check-site
python check_site.py https://www.example.com
```

## Exemples

```bash
# Site en ligne, avec les contrôles propres à la production
python check_site.py https://www.example.com --prod --out rapport.md

# Beta protégée par mot de passe
python check_site.py https://beta.example.com --auth utilisateur:motdepasse

# Refonte : tester chaque ligne du plan de redirections
python check_site.py https://www.example.com --prod --redirections redirections.csv

# Rapport JSON pour un autre outil
python check_site.py https://www.example.com --json --out rapport.json
```

## Les 43 contrôles

| Famille | Contrôles |
|---|---|
| Technique | erreurs serveur, liens internes cassés ou qui passent par une redirection, chaînes de redirections, temps de réponse, vraie réponse 404, page 404 personnalisée, pages vides en 200, fichiers liés en erreur |
| Indexation | `noindex`, canonical, robots.txt, sitemap XML (absent, illisible, URLs non canoniques, pages oubliées), beta ouverte aux moteurs, pages orphelines, paramètres d'URL, pagination |
| Contenu, entre les pages | titles et meta descriptions en double, H1, hiérarchie des titres, **intertitres répétés d'une page à l'autre**, intertitres génériques, **questions de FAQ répétées**, contenu faible, texte de remplissage (lorem ipsum, « à compléter »), textes de lien non descriptifs |
| Mots-clés | cannibalisation : deux pages avec le même mot-clé principal (option `--arbo`) |
| Images et partage | `alt`, dimensions, formats (WebP/AVIF), poids, Open Graph, polices tierces |
| GEO (moteurs IA) | données structurées JSON-LD, `hreflang`, langue de la page, `nosnippet` et `max-snippet` |
| Production (`--prod`) | variantes http et www en une seule 301, HSTS |
| Refonte | chaque ligne du plan de redirections (option `--redirections`) |

Les contrôles sont automatiques : la pertinence d'un intertitre ou d'une réponse se juge toujours à la lecture.

## Options

| Option | Rôle |
|---|---|
| `--auth USER:PASS` | Auth HTTP d'une beta protégée (ou variable d'environnement `CHECK_SITE_AUTH`) |
| `--prod` | Contrôles de production : `noindex` et `Disallow: /` deviennent des erreurs, variantes http/www et HSTS vérifiés |
| `--seeds FICHIER` | URLs de départ en plus : export Screaming Frog (colonne `Address`) ou une URL par ligne. Celles qu'aucun lien interne n'atteint sont signalées comme orphelines |
| `--arbo FICHIER` | Arborescence en Markdown : le premier tableau qui a une colonne `URL` et une colonne `Keyword` (ou `Mot-clé principal`) |
| `--redirections FICHIER` | Plan de redirections en CSV séparé par `;`, colonnes `old_url;new_url;code` (ou `ancienne_url;nouvelle_url;code`) |
| `--max-pages N` | Limite de l'exploration (500 par défaut) |
| `--delay S` | Pause entre deux requêtes, en secondes (0,2 par défaut) |
| `--out FICHIER` | Écrit le rapport dans un fichier au lieu de l'afficher |
| `--json` | Rapport JSON au lieu du Markdown |
| `--passive` | Se comporte comme un robot : respecte les `Disallow` du robots.txt |
| `--user-agent UA` | User-Agent envoyé (`check_site/1.0 (+site audit, read-only)` par défaut) |

Exemple de fichier pour `--arbo` :

```markdown
| URL | Keyword |
|---|---|
| /creation-site | création site internet |
| /refonte | refonte site internet |
```

Exemple de fichier pour `--redirections` :

```csv
old_url;new_url;code
/ancienne-page;/nouvelle-page;301
/page-supprimee;;410
```

## Codes de sortie

- `0` : aucune erreur ;
- `1` : au moins une erreur (pratique en intégration continue) ;
- `2` : mauvaise utilisation, ou page d'accueil injoignable.

Avec `--json`, le code vaut `0` dès que l'exploration a tourné, et `2` sur une erreur fatale.

## Format JSON (`check-site/1`)

```json
{
  "schema": "check-site/1",
  "tool": "check_site",
  "version": "2.2.0",
  "target": "https://www.example.com/",
  "mode": "active",
  "started_at": "2026-11-18T23:00:00+01:00",
  "finished_at": "2026-11-18T23:01:12+01:00",
  "stats": { "pages": 42, "requests": 97, "truncated": false, "robots_blocked": 0 },
  "findings": [
    {
      "id": "SEO-12.title-duplicate", "grid": "SEO-12", "category": "seo", "severity": "recommended", "level_raw": "avertissement",
      "title": "Titles en double", "subject": null, "count": 2,
      "urls": ["https://www.example.com/a", "https://www.example.com/b"], "evidence": ["…"]
    }
  ],
  "passed": [{ "grid": "SEO-14", "detail": "H1 : aucun problème détecté par ce test" }],
  "unverified": [{ "grid": "SEO-20", "reason": "option --arbo non fournie (arborescence du projet)" }]
}
```

- `findings` : les défauts trouvés, du plus grave au moins grave. `grid` est le numéro du contrôle (SEO-01 à SEO-43) ; `severity` vaut `important` (erreur), `recommended` (avertissement) ou `info` ; `category` vaut `seo`, `performance`, `accessibilite` ou `securite`.
- `passed` : les contrôles passés sans défaut.
- `unverified` : les contrôles qui n'ont pas pu tourner, et pourquoi.

## Tests

```bash
python -m unittest discover -s tests
```

Le test monte un petit site local avec des défauts connus (title en double, image sans `alt`, lien cassé) et vérifie que le rapport les trouve.

## Licence

MIT. Développé par [DataPet](https://datapet.fr).
