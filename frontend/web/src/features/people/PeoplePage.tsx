import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Check,
  ExternalLink,
  ImageOff,
  LoaderCircle,
  Search,
  Split,
  UserRound,
  UsersRound,
  X,
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { useDurableMutation } from "../../api/mutations";
import type { FaceMutationResult, LibraryFilters, PersonSummary } from "../../api/types";
import { Button } from "../../components/Button";
import { useToast } from "../../components/Toast";
import { writeFilters } from "../../domain/library";
import { FaceThumbnail } from "./FaceThumbnail";

type PeopleFilter = "all" | "named" | "unnamed";

function personLabel(person: PersonSummary) {
  return person.displayName || `Unnamed group · ${person.personId.slice(0, 6)}`;
}

function PersonRow({
  person,
  active,
  onClick,
}: {
  person: PersonSummary;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className={`person-row ${active ? "is-active" : ""}`}
      onClick={onClick}
      aria-pressed={active}
    >
      <span className="person-row__faces" aria-hidden="true">
        {person.sampleFaces.slice(0, 4).map((face) => <FaceThumbnail key={face.faceId} face={face} />)}
      </span>
      <span className="person-row__copy">
        <strong>{person.displayName || "Unnamed person"}</strong>
        <small>{person.faceCount} face{person.faceCount === 1 ? "" : "s"} · {person.photoCount} photo{person.photoCount === 1 ? "" : "s"}</small>
      </span>
    </button>
  );
}

export function PeoplePage({ filters }: { filters: LibraryFilters }) {
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<PeopleFilter>("all");
  const [personId, setPersonId] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [name, setName] = useState("");
  const [mergeTarget, setMergeTarget] = useState("");
  const [moveTarget, setMoveTarget] = useState("new");
  const [busy, setBusy] = useState(false);
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const toast = useToast();
  const { mutate } = useDurableMutation();

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  const peopleQuery = useQuery({
    queryKey: ["people", query],
    queryFn: ({ signal }) => api.people(query, signal),
  });
  const people = peopleQuery.data?.items ?? [];
  const visiblePeople = useMemo(
    () => people.filter((person) => filter === "all" || (filter === "named" ? person.displayName : !person.displayName)),
    [filter, people],
  );

  useEffect(() => {
    if (visiblePeople.length === 0) {
      setPersonId(null);
    } else if (!personId || !visiblePeople.some((person) => person.personId === personId)) {
      setPersonId(visiblePeople[0].personId);
    }
  }, [personId, visiblePeople]);

  const detailQuery = useQuery({
    queryKey: ["person", personId],
    queryFn: ({ signal }) => api.person(personId!, signal),
    enabled: Boolean(personId),
  });
  const detail = detailQuery.data;

  useEffect(() => {
    setSelected(new Set());
    setMoveTarget("new");
    setMergeTarget("");
  }, [personId]);
  useEffect(() => setName(detail?.displayName ?? ""), [detail?.displayName, detail?.personId]);

  const refreshPeople = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["people"] }),
      queryClient.invalidateQueries({ queryKey: ["person"] }),
      queryClient.invalidateQueries({ queryKey: ["photo"] }),
      queryClient.invalidateQueries({ queryKey: ["library"] }),
    ]);
  };

  const saveName = async (event: FormEvent) => {
    event.preventDefault();
    if (!personId || name.trim() === (detail?.displayName ?? "")) return;
    setBusy(true);
    try {
      await mutate<FaceMutationResult>(`/people/${personId}`, "PATCH", { displayName: name.trim() });
      await refreshPeople();
      toast.show(name.trim() ? `Named ${name.trim()}` : "Name cleared");
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Name could not be saved", "error");
    } finally {
      setBusy(false);
    }
  };

  const mergePerson = async () => {
    if (!personId || !mergeTarget) return;
    const source = people.find((person) => person.personId === personId);
    const target = people.find((person) => person.personId === mergeTarget);
    if (!source || !target) return;
    if (!window.confirm(`Combine ${personLabel(source)} into ${personLabel(target)}? The destination name will be kept.`)) return;
    setBusy(true);
    try {
      const result = await mutate<FaceMutationResult>(`/people/${personId}/merge`, "POST", {
        targetPersonId: mergeTarget,
      });
      setPersonId(result.personId);
      await refreshPeople();
      toast.show("Face groups combined");
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Groups could not be combined", "error");
    } finally {
      setBusy(false);
    }
  };

  const moveFaces = async () => {
    if (!personId || selected.size === 0) return;
    setBusy(true);
    try {
      const result = await mutate<FaceMutationResult>("/faces/move", "POST", {
        faceIds: [...selected],
        targetPersonId: moveTarget === "new" ? null : moveTarget,
      });
      const movedEverything = selected.size === detail?.faceCount;
      setSelected(new Set());
      if (movedEverything) setPersonId(result.personId);
      await refreshPeople();
      toast.show(
        moveTarget === "new"
          ? `${result.movedFaces ?? 0} face${result.movedFaces === 1 ? "" : "s"} separated into a new group`
          : `${result.movedFaces ?? 0} face${result.movedFaces === 1 ? "" : "s"} reassigned`,
      );
    } catch (error) {
      toast.show(error instanceof Error ? error.message : "Faces could not be moved", "error");
    } finally {
      setBusy(false);
    }
  };

  const toggleFace = (faceId: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(faceId)) next.delete(faceId);
      else next.add(faceId);
      return next;
    });
  };

  const openPhoto = (assetId: string) => {
    navigate({ pathname: `/photo/${assetId}`, search: writeFilters(filters).toString() });
  };

  const counts = peopleQuery.data;
  const otherPeople = people.filter((person) => person.personId !== personId);

  return (
    <section className="people-workspace">
      <header className="people-header">
        <div>
          <p className="eyebrow">Face catalog</p>
          <h1>People</h1>
          <p>
            {counts ? `${counts.total} group${counts.total === 1 ? "" : "s"} · ${counts.named} named · ${counts.unnamed} to review` : "Reading detected faces…"}
          </p>
        </div>
        <div className="people-header__hint"><UsersRound size={15} /><span>Name, combine, and correct detected face groups</span></div>
      </header>

      <div className="people-tools">
        <label className="search-field">
          <Search size={15} />
          <input value={search} onChange={(event) => setSearch(event.target.value)} type="search" placeholder="Find a person or photograph…" aria-label="Search people" />
          {search && <button type="button" onClick={() => setSearch("")} aria-label="Clear people search"><X size={14} /></button>}
        </label>
        <div className="segmented-control" aria-label="Filter face groups">
          {(["all", "named", "unnamed"] as const).map((value) => (
            <button type="button" key={value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>
              {value === "all" ? "All groups" : value === "named" ? "Named" : "Unnamed"}
            </button>
          ))}
        </div>
      </div>

      <div className="people-layout">
        <aside className="people-groups" aria-label="Face groups">
          <header><span>Groups</span><small>{visiblePeople.length}</small></header>
          <div className="people-groups__scroll" aria-busy={peopleQuery.isLoading}>
            {peopleQuery.isLoading && <div className="people-pane-state"><LoaderCircle className="spin" size={18} /> Loading groups</div>}
            {peopleQuery.isError && <div className="people-pane-state people-pane-state--error"><ImageOff size={20} /> Face groups unavailable</div>}
            {!peopleQuery.isLoading && !peopleQuery.isError && visiblePeople.length === 0 && (
              <div className="people-pane-state"><UserRound size={22} />{query || filter !== "all" ? "No matching groups" : "No faces detected yet"}</div>
            )}
            {visiblePeople.map((person) => (
              <PersonRow key={person.personId} person={person} active={person.personId === personId} onClick={() => setPersonId(person.personId)} />
            ))}
          </div>
        </aside>

        <main className="face-review" aria-busy={detailQuery.isLoading}>
          {detailQuery.isLoading && <div className="center-state"><LoaderCircle className="spin" /><span>Loading faces</span></div>}
          {!detailQuery.isLoading && !detail && (
            <div className="center-state people-empty">
              <UsersRound size={38} />
              <h2>Select a face group</h2>
              <p>Detected people appear here after local photo analysis finishes.</p>
            </div>
          )}
          {detail && (
            <>
              <header className="face-review__header">
                <div>
                  <span className="eyebrow">Review group</span>
                  <h2>{detail.displayName || "Unnamed person"}</h2>
                  <p>{detail.faceCount} face{detail.faceCount === 1 ? "" : "s"} across {detail.photoCount} photograph{detail.photoCount === 1 ? "" : "s"}</p>
                </div>
                <div className="face-review__selection">
                  <button type="button" onClick={() => setSelected(new Set(detail.faces.map((face) => face.faceId)))}>Select all</button>
                  {selected.size > 0 && <button type="button" onClick={() => setSelected(new Set())}>Clear</button>}
                </div>
              </header>
              <div className="face-grid">
                {detail.faces.map((face) => {
                  const active = selected.has(face.faceId);
                  return (
                    <article className={`face-tile ${active ? "is-selected" : ""}`} key={face.faceId}>
                      <button type="button" className="face-tile__choose" onClick={() => toggleFace(face.faceId)} aria-pressed={active} aria-label={`${active ? "Deselect" : "Select"} face in ${face.originalFilename}`}>
                        <FaceThumbnail face={face} alt={`Detected face in ${face.originalFilename}`} />
                        <span className="face-tile__check">{active && <Check size={13} />}</span>
                      </button>
                      <footer>
                        <span title={face.originalFilename}>{face.originalFilename}</span>
                        <button type="button" onClick={() => openPhoto(face.assetId)} title="Open photograph" aria-label={`Open ${face.originalFilename}`}><ExternalLink size={12} /></button>
                      </footer>
                    </article>
                  );
                })}
              </div>
              {detail.faces.length < detail.faceCount && <div className="face-limit-note">Showing the first {detail.faces.length.toLocaleString()} faces in this group.</div>}
            </>
          )}
        </main>

        <aside className="people-inspector" aria-label="Person tools">
          <header><UserRound size={14} /><span>Person tools</span></header>
          {!detail ? (
            <p className="people-inspector__empty">Choose a group to edit its identity and assignments.</p>
          ) : (
            <>
              <section className="people-tool-section">
                <h3>Identity</h3>
                <form onSubmit={saveName} className="person-name-form">
                  <label className="field"><span>Name</span><input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} placeholder="Unnamed person" /></label>
                  <Button compact tone="primary" disabled={busy || name.trim() === detail.displayName}>Save name</Button>
                </form>
                <p>Clear the name to return this group to the review queue.</p>
              </section>

              <section className="people-tool-section">
                <h3>Correct assignments</h3>
                <p>Select faces in the grid, then move them out of this group. Use a new group to split a mistaken match.</p>
                <label className="field"><span>Move selected to</span>
                  <select value={moveTarget} onChange={(event) => setMoveTarget(event.target.value)}>
                    <option value="new">New unnamed group</option>
                    {otherPeople.map((person) => <option key={person.personId} value={person.personId}>{personLabel(person)}</option>)}
                  </select>
                </label>
                <Button compact disabled={busy || selected.size === 0} onClick={moveFaces}>
                  <Split size={13} /> {selected.size > 0 ? `Move ${selected.size} selected` : "Select faces to move"}
                </Button>
              </section>

              <section className="people-tool-section">
                <h3>Combine groups</h3>
                <p>Move every face in this group into another group. The destination name is kept.</p>
                <label className="field"><span>Combine into</span>
                  <select value={mergeTarget} onChange={(event) => setMergeTarget(event.target.value)}>
                    <option value="">Choose a group…</option>
                    {otherPeople.map((person) => <option key={person.personId} value={person.personId}>{personLabel(person)}</option>)}
                  </select>
                </label>
                <Button compact disabled={busy || !mergeTarget} onClick={mergePerson}><UsersRound size={13} /> Combine groups</Button>
              </section>
            </>
          )}
        </aside>
      </div>

      <footer className="people-statusbar">
        <span>{selected.size > 0 ? <><strong>{selected.size}</strong> face{selected.size === 1 ? "" : "s"} selected</> : detail ? `${detail.faces.length.toLocaleString()} faces loaded` : `${visiblePeople.length.toLocaleString()} groups`}</span>
        <span>Edits save to the local catalog</span>
      </footer>
    </section>
  );
}
