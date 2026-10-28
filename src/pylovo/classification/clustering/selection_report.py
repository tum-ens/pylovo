"""Selection report of a classification: how close and how stable the choice of parameters and of k is.

Step 6 keeps one factor per eigenvalue above 1 (Kaiser criterion), takes the parameter with the largest varimax
loading of every factor and the number of clusters with the best Calinski-Harabasz (CH) index. Each rule turns on a
threshold or a maximum, so a small change of the sample can change the choice. The report repeats these steps on the
sample and on PLZ bootstrap resamples (the PLZ of the sample drawn with replacement, with all their grids). It lists
the adequacy checks of the factor analysis (Bartlett's test, Kaiser-Meyer-Olkin criterion), the eigenvalues, the
margin of every pick, the CH and Davies-Bouldin (DB) curves, how often each choice repeats, and a warning for every
close call. It changes no choice.
"""
import warnings
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd
from factor_analyzer.factor_analyzer import calculate_bartlett_sphericity, calculate_kmo
from sklearn.cluster import KMeans
from sklearn.metrics import calinski_harabasz_score, davies_bouldin_score
from sklearn.mixture import GaussianMixture

from pylovo.classification.clustering.clustering_algorithms import scaled_key_figures
from pylovo.classification.clustering.get_parameters_for_clustering import candidate_eigenvalues, factor_analysis_input
from pylovo.classification.clustering.transform import transform_key_figures
from pylovo.classification.database_communication.database_communication import DatabaseCommunication
from pylovo.config_loader import NO_OF_CLUSTERS_ALLOWED, SELECTION_BOOTSTRAP
from pylovo.plotting.classification import varimax_factor_analysis

#: KMO below this: the candidates are poorly suited for a factor analysis
KMO_MIN = 0.6
#: an eigenvalue this close to 1 decides the number of factors by a hair
EIGENVALUE_MARGIN = 0.1
#: a pick that leads the runner-up of its factor by less is a near tie
LOADING_MARGIN = 0.05
#: a uniqueness below this: the factors explain all of the variance of the parameter (Heywood case)
HEYWOOD_UNIQUENESS = 0.01
#: a choice that fewer bootstrap resamples repeat is unstable
STABLE_SHARE = 0.8
#: another number of clusters within this share of the best CH index is a near tie
CH_NEAR_TIE = 0.02

#: The two algorithms of step 7 with their seeds (as in ``clustering_algorithms`` and the CH curves)
ALGORITHMS = {
    "kmeans": lambda k: KMeans(n_clusters=k, random_state=0),
    "gmm_tied": lambda k: GaussianMixture(n_components=k, covariance_type="tied", random_state=1),
}


def _selection(df_parameters: pd.DataFrame, transform: str) -> dict:
    """Step 6a on one sample: transformed candidates, eigenvalues, number of factors, loadings and picks."""
    candidates = transform_key_figures(factor_analysis_input(df_parameters), transform)
    eigenvalues = candidate_eigenvalues(candidates)
    n_factors = max(int((eigenvalues > 1).sum()), 1)
    loadings, model = varimax_factor_analysis(candidates, n_factors)
    return {"candidates": candidates, "eigenvalues": eigenvalues, "n_factors": n_factors, "loadings": loadings,
            "model": model, "picks": [str(loadings[factor].abs().idxmax()) for factor in loadings]}


def _cluster_indices(df_parameters: pd.DataFrame, parameters: list, transform: str,
                     no_of_clusters_allowed) -> dict[str, pd.DataFrame]:
    """Step 6b on one sample: CH and DB index of every algorithm and number of clusters (NaN for a degenerate fit)."""
    X = scaled_key_figures(df_parameters, parameters, transform)
    tables = {}
    for name, make in ALGORITHMS.items():
        rows = []
        for k in no_of_clusters_allowed:
            model = make(k).fit(X)
            labels = model.labels_ if hasattr(model, "labels_") else model.predict(X)
            try:
                rows.append((k, calinski_harabasz_score(X, labels), davies_bouldin_score(X, labels)))
            except ValueError:                      # a mixture with a single occupied component
                rows.append((k, np.nan, np.nan))
        tables[name] = pd.DataFrame(rows, columns=["k", "ch", "db"]).set_index("k")
    return tables


def _resample(df_parameters: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Draw the PLZ of the sample with replacement and return all grids of the drawn PLZ."""
    groups = list(df_parameters.groupby("plz").indices.values())
    drawn = rng.integers(0, len(groups), size=len(groups))
    return df_parameters.iloc[np.concatenate([groups[i] for i in drawn])].reset_index(drop=True)


def _shares(values) -> dict[str, float]:
    """Share of every value, most frequent first, keyed by its text."""
    counts = Counter(values)
    total = sum(counts.values())
    return {str(value): round(count / total, 3) for value, count in counts.most_common()}


def selection_report(df_parameters: pd.DataFrame, list_of_clustering_parameters: list | None = None,
                     transform: str = "yeo-johnson", resamples: int = SELECTION_BOOTSTRAP,
                     no_of_clusters_allowed=NO_OF_CLUSTERS_ALLOWED, seed: int = 0) -> dict:
    """Return the selection report of the grids ``df_parameters``.

    Args:
        df_parameters: Key figures of the grids that step 6 reads, with the column ``plz``.
        list_of_clustering_parameters: The parameters the CH and DB curves are computed for; ``None`` takes the
            automatic choice.
        transform: Transform of the key figures (:func:`transform_key_figures`).
        resamples: Number of PLZ bootstrap resamples; 0 computes the report of the sample only.
        no_of_clusters_allowed: Numbers of clusters of the curves.
        seed: Seed of the resampling.

    Returns:
        dict: JSON-ready report: ``grids``, ``plz``, ``transform``, ``lambdas``, ``adequacy``, ``eigenvalues``,
        ``factors``, ``explained_variance``, ``factor_analysis``, ``picks``, ``parameters``, ``clusters``,
        ``bootstrap`` and ``warnings``.
    """
    allowed = list(no_of_clusters_allowed)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # convergence and deprecation warnings of the fits; reported below
        full = _selection(df_parameters, transform)
        parameters = list(list_of_clustering_parameters or full["picks"])
        indices = _cluster_indices(df_parameters, parameters, transform, allowed)
        chi2, p_value = calculate_bartlett_sphericity(full["candidates"])
        _, kmo = calculate_kmo(full["candidates"])
        rng = np.random.default_rng(seed)
        runs = []
        for _ in range(resamples):
            sample = _resample(df_parameters, rng)
            selection = _selection(sample, transform)
            curves = _cluster_indices(sample, parameters, transform, allowed)
            runs.append({"eigenvalues": selection["eigenvalues"], "n_factors": selection["n_factors"],
                         "picks": selection["picks"],
                         "best": {name: (int(t["ch"].idxmax()), int(t["db"].idxmin())) for name, t in curves.items()}})

    eigenvalues = full["eigenvalues"]
    n_factors = full["n_factors"]
    model = full["model"]
    uniqueness = pd.Series(model.noise_variance_, index=full["candidates"].columns)
    report_warnings = []
    if kmo < KMO_MIN:
        report_warnings.append(f"KMO {kmo:.2f} is below {KMO_MIN}: the candidates are poorly suited for a factor "
                               "analysis.")
    if p_value > 0.05:
        report_warnings.append(f"Bartlett's test does not reject uncorrelated candidates (p = {p_value:.3f}).")
    for rank in (n_factors, n_factors + 1):
        if rank <= len(eigenvalues) and abs(eigenvalues[rank - 1] - 1) < EIGENVALUE_MARGIN:
            report_warnings.append(f"Eigenvalue {rank} is {eigenvalues[rank - 1]:.3f}, within {EIGENVALUE_MARGIN} of "
                                   f"the cut-off 1: the number of factors ({n_factors}) is borderline.")
    if model.n_iter_ >= model.max_iter:
        report_warnings.append(f"The factor analysis stopped at its iteration limit ({model.max_iter}).")
    for parameter, value in uniqueness[uniqueness < HEYWOOD_UNIQUENESS].items():
        report_warnings.append(f"The uniqueness of {parameter} is {value:.4f}: the factors explain all of its variance "
                               "(Heywood case), so its loadings sit at their limit.")

    in_selection = Counter(pick for run in runs for pick in set(run["picks"]))
    picks = []
    for factor in full["loadings"]:
        ordered = full["loadings"][factor].abs().sort_values(ascending=False)
        pick = {"factor": int(factor) + 1, "pick": str(ordered.index[0]), "loading": round(float(ordered.iloc[0]), 3),
                "runner_up": str(ordered.index[1]), "runner_up_loading": round(float(ordered.iloc[1]), 3),
                "margin": round(float(ordered.iloc[0] - ordered.iloc[1]), 3),
                "bootstrap_share": round(in_selection[ordered.index[0]] / len(runs), 3) if runs else None}
        picks.append(pick)
        if pick["margin"] < LOADING_MARGIN:
            report_warnings.append(f"{pick['pick']} leads {pick['runner_up']} on factor {pick['factor']} by only "
                                   f"{pick['margin']:.3f}: either is defensible.")
        if runs and pick["bootstrap_share"] < STABLE_SHARE:
            report_warnings.append(f"{pick['pick']} is picked in {pick['bootstrap_share']:.0%} of the resamples only.")

    clusters = {}
    for name, table in indices.items():
        best_ch, best_db = int(table["ch"].idxmax()), int(table["db"].idxmin())
        near = [int(k) for k in table.index if k != best_ch and table.at[k, "ch"] >= (1 - CH_NEAR_TIE) * table.at[best_ch, "ch"]]
        clusters[name] = {"ch": {str(k): round(float(v), 1) for k, v in table["ch"].items()},
                          "db": {str(k): round(float(v), 3) for k, v in table["db"].items()},
                          "best_ch": best_ch, "best_db": best_db, "near_ties_ch": near}
        if near:
            report_warnings.append(f"{name}: k = {', '.join(map(str, near))} within {CH_NEAR_TIE:.0%} of the best CH "
                                   f"index (k = {best_ch}).")
        if best_db != best_ch:
            report_warnings.append(f"{name}: the DB index prefers k = {best_db}, the CH index k = {best_ch}.")

    bootstrap = {"resamples": len(runs), "seed": seed}
    if runs:
        factor_counts = _shares(run["n_factors"] for run in runs)
        ranks = min(len(eigenvalues), n_factors + 2)
        sampled = np.array([run["eigenvalues"][:ranks] for run in runs])
        bootstrap.update({
            "factor_counts": factor_counts,
            "eigenvalue_ranges": [{"rank": rank + 1, "low": round(float(low), 3), "median": round(float(mid), 3),
                                   "high": round(float(high), 3)}
                                  for rank, (low, mid, high) in enumerate(np.percentile(sampled, [2.5, 50, 97.5], axis=0).T)],
            "selections": _shares(", ".join(sorted(run["picks"])) for run in runs),
            "best_k": {name: {"ch": _shares(run["best"][name][0] for run in runs),
                              "db": _shares(run["best"][name][1] for run in runs)} for name in ALGORITHMS},
        })
        if factor_counts.get(str(n_factors), 0) < STABLE_SHARE:
            report_warnings.append(f"The resamples keep {n_factors} factors in {factor_counts.get(str(n_factors), 0):.0%} "
                                   "of the cases only.")
        for name in ALGORITHMS:
            share = bootstrap["best_k"][name]["ch"].get(str(clusters[name]["best_ch"]), 0)
            if share < STABLE_SHARE:
                report_warnings.append(f"{name}: the resamples repeat the best CH k = {clusters[name]['best_ch']} in "
                                       f"{share:.0%} of the cases only.")

    return {
        "grids": int(len(df_parameters)), "plz": int(df_parameters["plz"].nunique()), "transform": transform,
        "lambdas": {name: round(value, 3) for name, value in full["candidates"].attrs.get("lambdas", {}).items()},
        "adequacy": {"bartlett_chi2": round(float(chi2), 1), "bartlett_p": float(p_value), "kmo": round(float(kmo), 3)},
        "eigenvalues": [round(float(value), 3) for value in eigenvalues],
        "factors": n_factors,
        "explained_variance": round(float(eigenvalues[:n_factors].sum() / len(eigenvalues)), 3),
        "factor_analysis": {"iterations": int(model.n_iter_),
                            "smallest_uniqueness": {str(name): round(float(value), 4)
                                                    for name, value in uniqueness.nsmallest(3).items()}},
        "picks": picks,
        "parameters": parameters,
        "clusters": clusters,
        "bootstrap": bootstrap,
        "warnings": report_warnings,
    }


def format_selection_report(report: dict, classification_id: int | None = None) -> str:
    """Return the report of :func:`selection_report` as Markdown."""
    title = "Selection report" + (f" of classification {classification_id}" if classification_id is not None else "")
    adequacy = report["adequacy"]
    lines = [f"# {title}", "",
             f"{report['grids']} grids in {report['plz']} PLZ, transform {report['transform']}, "
             f"{report['bootstrap']['resamples']} PLZ bootstrap resamples (seed {report['bootstrap']['seed']}).", "",
             "## Factor analysis", "",
             f"- Bartlett's test: chi2 {adequacy['bartlett_chi2']}, p {adequacy['bartlett_p']:.3g}. "
             f"KMO {adequacy['kmo']} (needs {KMO_MIN} or more).",
             f"- Eigenvalues: {', '.join(map(str, report['eigenvalues']))}. {report['factors']} factors explain "
             f"{report['explained_variance']:.0%} of the variance.",
             f"- Iterations of the varimax factor analysis: {report['factor_analysis']['iterations']}. Smallest "
             "uniqueness: " + ", ".join(f"{name} {value}"
                                        for name, value in report["factor_analysis"]["smallest_uniqueness"].items())
             + "."]
    if report["lambdas"]:
        lines.append("- Yeo-Johnson exponents: " + ", ".join(f"{name} {value}" for name, value in report["lambdas"].items())
                     + ".")
    lines += ["", "| Factor | Pick | Loading | Runner-up | Loading | Margin | Picked in resamples |",
              "|---|---|---|---|---|---|---|"]
    for pick in report["picks"]:
        share = "–" if pick["bootstrap_share"] is None else f"{pick['bootstrap_share']:.0%}"
        lines.append(f"| {pick['factor']} | {pick['pick']} | {pick['loading']} | {pick['runner_up']} | "
                     f"{pick['runner_up_loading']} | {pick['margin']} | {share} |")
    lines += ["", f"## Number of clusters ({', '.join(report['parameters'])})", "",
              "| k | " + " | ".join(f"CH {name} | DB {name}" for name in report["clusters"]) + " |",
              "|---|" + "---|---|" * len(report["clusters"])]
    for k in next(iter(report["clusters"].values()))["ch"]:
        lines.append(f"| {k} | " + " | ".join(f"{c['ch'][k]} | {c['db'][k]}" for c in report["clusters"].values()) + " |")
    lines.append("")
    lines += [f"- {name}: best CH k = {c['best_ch']}, best DB k = {c['best_db']}." for name, c in report["clusters"].items()]
    boot = report["bootstrap"]
    if boot["resamples"]:
        lines += ["", "## Bootstrap", "",
                  "- Number of factors: " + ", ".join(f"{n}: {share:.0%}" for n, share in boot["factor_counts"].items()) + ".",
                  "- Eigenvalues (2.5 %, median, 97.5 %): "
                  + "; ".join(f"{r['rank']}: {r['low']}, {r['median']}, {r['high']}" for r in boot["eigenvalue_ranges"]) + ".",
                  "- Selections: " + "; ".join(f"{sel} ({share:.0%})" for sel, share in list(boot["selections"].items())[:5]) + "."]
        for name, best in boot["best_k"].items():
            lines.append(f"- {name}, best k: CH " + ", ".join(f"{k}: {s:.0%}" for k, s in best["ch"].items())
                         + "; DB " + ", ".join(f"{k}: {s:.0%}" for k, s in best["db"].items()) + ".")
    lines += ["", "## Warnings", ""] + ([f"- {text}" for text in report["warnings"]] or ["- none"])
    return "\n".join(lines) + "\n"


def report_selection(list_of_clustering_parameters: list, additional_filtering: bool | None = None,
                     transform: str | None = None, resamples: int | None = None,
                     path: str | Path | None = None) -> dict:
    """Compute the selection report of the active classification, print it, write it and store it.

    The report goes to the standard output, to ``path`` (default ``log/selection_report_<classification>.md``), to
    ``classification_version.selection_report`` and its warnings to the log.

    Args:
        list_of_clustering_parameters: The chosen clustering parameters.
        additional_filtering: Filter choice of the grids read; ``None`` uses the choice of the classification.
        transform: Transform of the key figures; ``None`` uses the choice of the classification.
        resamples: PLZ bootstrap resamples; ``None`` uses ``SELECTION_BOOTSTRAP`` of ``config_classification.yaml``.
        path: File of the Markdown report.

    Returns:
        dict: The report of :func:`selection_report`.
    """
    dc = DatabaseCommunication()
    if transform is None:
        transform = dc.get_classification_choices()["transform"]
    df_parameters = dc.get_clustering_parameters_for_classification_version(additional_filtering)
    report = selection_report(df_parameters, list_of_clustering_parameters, transform,
                              SELECTION_BOOTSTRAP if resamples is None else resamples)
    text = format_selection_report(report, dc.classification_id)
    print(text)
    path = Path("log") / f"selection_report_{dc.classification_id}.md" if path is None else Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    dc.save_selection_report(report)
    for text in report["warnings"]:
        dc.dbc.logger.warning(f"Selection report: {text}")
    return report
