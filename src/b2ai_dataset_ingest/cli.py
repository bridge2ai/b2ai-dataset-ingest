"""Command-line interface for b2ai-dataset-ingest.

Usage (once implemented):

    b2ai-ingest voice \\
        --input data/synthetic/voice_dgp/b2ai-voice-synthetic-phenotype/output/phenotype \\
        --output out/ --target phenopacket

Synthetic input lives under ``data/synthetic/``; ``data/real/`` holds the source datasets and may be
owned by a separate account.
"""

from __future__ import annotations

import logging
from pathlib import Path

import typer

app = typer.Typer(
    add_completion=False,
    help="Ingest Bridge2AI datasets into GA4GH Phenopackets (and other future targets).",
)

# Registry of available output emitters, keyed by target name.
EMITTERS = {"phenopacket": "b2ai_dataset_ingest.emitters:PhenopacketEmitter"}


def _refuse_if_populated(output: Path, *, force: bool) -> list[Path]:
    """Refuse to write into a directory that already holds phenopackets.

    The emitter writes one file per participant, named by participant id. Re-running
    over an existing set therefore overwrites everyone still in the cohort but leaves
    a stale file behind for anyone who has since dropped out, so the directory becomes
    a silent union of two runs and nothing in the output says so.

    `--force` is named for what it overrides, not for what it does to each file:
    overwriting is already the default, and the leftovers it deletes are exactly the
    files that would *not* have been overwritten.
    """
    existing = sorted(output.glob("*.json")) if output.is_dir() else []
    if not existing:
        return []
    if not force:
        # Lead with what happened, not with what a re-run would have done. The
        # reader's first question is whether their existing output survived.
        typer.echo(
            f"Refused: nothing was written. The {len(existing)} phenopacket(s) already "
            f"in {output} are unchanged.\n"
            "\n"
            "Why: the ingest writes one file per participant. Re-running here would "
            "overwrite everyone still in the cohort but leave a stale file behind for "
            "anyone who has since dropped out, so the directory would become a silent "
            "mix of two runs.\n"
            "\n"
            f"--force DELETES all {len(existing)} existing .json file(s) in {output}, "
            "permanently and with no backup, and then writes the new cohort. It does "
            "NOT merge the two.\n"
            "\n"
            "To keep what is there, point --output at an empty directory instead.",
            err=True,
        )
        raise typer.Exit(code=2)
    return existing


def _normalize_hpo(participants, enabled: bool, hpo_json: Path | None):
    """Collapse redundant HPO annotations, or return None when not asked to.

    Every failure here is fatal rather than a warning. Collapsing is destructive and
    is decided by the ontology's subsumptions, so falling back to un-normalized
    output on a missing dependency, a missing file, or the wrong release would give
    a run that looks like it normalized and did not.
    """
    if not enabled:
        if hpo_json is not None:
            typer.echo("--hpo-json has no effect without --normalize-hpo", err=True)
        return None

    from b2ai_dataset_ingest.ontology.hpo_coherence import (
        OntologyUnavailable,
        OntologyVersionMismatch,
        collapse_all,
        declared_hpo_version,
        load_ontology,
        require_version,
    )

    if hpo_json is None:
        typer.echo(
            "--normalize-hpo needs --hpo-json.\n"
            "It must be the same hp.json any downstream clustering reads, so a term is "
            "never collapsed on the strength of a subsumption that clustering does not "
            "share. scripts/cluster_phenopackets.sh keeps one at .stratiphy/hp.json.",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        ontology = load_ontology(hpo_json)
        require_version(ontology, declared_hpo_version())
    except (OntologyUnavailable, OntologyVersionMismatch) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    return collapse_all(participants, ontology)


def _remove(existing: list[Path]) -> None:
    """Delete the previous set. Call this only once the new one is in hand.

    Deleting before reading the source would mean a failed ingest leaves the
    caller with neither: the old cohort gone and no new one written. On a real
    cohort that is not trivially regenerable, so the refusal check runs early
    and the deletion runs late.
    """
    if not existing:
        return
    for path in existing:
        path.unlink()
    typer.echo(
        f"--force: permanently deleted {len(existing)} previous phenopacket(s); "
        "writing a fresh set"
    )


@app.command()
def voice(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the voice phenotype/ dir."),
    output: Path = typer.Option(..., "--output", "-o", help="Output directory."),
    config: Path = typer.Option(
        Path("config/voice"), "--config", "-c", help="Mapping config dir."
    ),
    target: str = typer.Option("phenopacket", "--target", "-t", help="Output target."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Permanently delete existing phenopackets in --output, then write a "
        "fresh set. Does not merge.",
    ),
    questionnaires: str = typer.Option(
        "",
        "--questionnaires",
        help="Comma-separated questionnaire table names to use, e.g. "
        "phq9,gad7_anxiety,vhi10. Others are not ingested. Every HPO term comes "
        "from a questionnaire, so this is what equalises phenotype coverage.",
    ),
    require_all_questionnaires: bool = typer.Option(
        False,
        "--require-all-questionnaires",
        help="With --questionnaires, emit only participants who were offered every "
        "one of them. Offered means a row exists, answered or not.",
    ),
    normalize_hpo: bool = typer.Option(
        False,
        "--normalize-hpo",
        help="Collapse an HPO term asserted alongside its own ancestor, keeping the "
        "more specific term and merging the ancestor's evidence into it. Requires "
        "--hpo-json and the 'hpo' extra.",
    ),
    hpo_json: Path = typer.Option(
        None,
        "--hpo-json",
        help="Path to the hp.json used for --normalize-hpo. Must be the same release "
        "the mappings declare, and the same file any downstream clustering reads.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-table warnings."),
) -> None:
    """Ingest the Bridge2AI-Voice dataset into one phenopacket per participant."""
    from b2ai_dataset_ingest.emitters import PhenopacketEmitter
    from b2ai_dataset_ingest.sources.voice import VoiceSource

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if target not in EMITTERS:
        typer.echo(f"unknown target {target!r}; available: {', '.join(EMITTERS)}", err=True)
        raise typer.Exit(code=2)
    if target != "phenopacket":  # only the phenopacket emitter is wired in v1
        typer.echo(f"target {target!r} is not implemented yet", err=True)
        raise typer.Exit(code=2)

    # Refuse early, delete late: a failure between the two must not leave the
    # caller with neither the old cohort nor a new one.
    existing = _refuse_if_populated(output, force=force)

    wanted = [q.strip() for q in questionnaires.split(",") if q.strip()]
    if require_all_questionnaires and not wanted:
        typer.echo(
            "--require-all-questionnaires needs --questionnaires: there is no battery "
            "to be incomplete against without one.",
            err=True,
        )
        raise typer.Exit(code=2)

    source = VoiceSource(
        root=input,
        config_dir=config,
        questionnaires=wanted or None,
        require_all_questionnaires=require_all_questionnaires,
    )
    participants = list(source.read())

    collapse_report = _normalize_hpo(participants, normalize_hpo, hpo_json)

    _remove(existing)
    written = PhenopacketEmitter().write_all(participants, output)
    typer.echo(f"Wrote {written} phenopackets to {output}")
    # Aggregate, PHI-safe summary so silent degradation (skipped items, un-keyed sessions,
    # unmapped tables) is visible rather than hidden behind a reassuring file count.
    typer.echo(source.report.render())
    if collapse_report is not None:
        typer.echo(collapse_report.render())


@app.command()
def validate(
    input: Path = typer.Option(..., "--input", "-i", help="Path to the voice phenotype/ dir."),
    config: Path = typer.Option(
        Path("config/voice"), "--config", "-c", help="Mapping config dir."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-table warnings."),
) -> None:
    """Preflight-check the voice layout against its configs and data dictionaries.

    Reads only headers, dictionary keys, and aggregate cell counts — never raw values — so it
    is safe on the real, PHI-sensitive dataset. Exits non-zero if any contract error is found.
    """
    from b2ai_dataset_ingest.sources.voice.validate import validate_voice

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    report = validate_voice(root=input, config_dir=config)
    typer.echo(report.render())
    if report.errors:
        raise typer.Exit(code=1)


@app.command("validate-mappings")
def validate_mappings(
    mappings: Path = typer.Option(
        Path("mappings"), "--mappings", "-m", help="Directory of *.sssom.tsv files."
    ),
    data_root: Path = typer.Option(
        None,
        "--data-root",
        "-d",
        help="phenotype/ dir to check b2ai: subjects against (skipped if omitted).",
    ),
    strict_ontology: bool = typer.Option(
        False,
        "--strict-ontology",
        help="Fail if the oaklib HPO backend is unavailable (default: skip the HPO check).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log details."),
) -> None:
    """Validate the B2AI -> HPO SSSOM mappings (no hallucinated / drifted HPO terms).

    Structural checks always run. The HPO existence/label check runs when oaklib + the HPO
    SQLite are available (install the ``validation`` extra); ``--strict-ontology`` makes their
    absence an error. Subject columns are checked only when ``--data-root`` is given. Exits
    non-zero if any error is found.
    """
    from b2ai_dataset_ingest.ontology.sssom_validate import default_mapping_files, validate_paths

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    files = sorted(mappings.glob("*.sssom.tsv")) or default_mapping_files()
    if not files:
        typer.echo(f"no *.sssom.tsv files found under {mappings}", err=True)
        raise typer.Exit(code=2)
    result = validate_paths(
        files, data_root=data_root, check_ontology=True if strict_ontology else None
    )
    typer.echo(result.render())
    if result.errors:
        raise typer.Exit(code=1)


@app.command()
def targets() -> None:
    """List available output targets."""
    for name in EMITTERS:
        typer.echo(name)


def main() -> None:  # pragma: no cover - thin wrapper
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
