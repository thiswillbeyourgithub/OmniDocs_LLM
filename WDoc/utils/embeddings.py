"""
* Class used to create the embeddings.
* Loads and store embeddings for each document.
"""

from typing import List, Union, Optional, Any, Tuple, Callable
import hashlib
import os
import queue
import faiss
import random
import time
from pathlib import Path, PosixPath
from tqdm import tqdm
import threading
from joblib import Parallel, delayed
from functools import wraps

import numpy as np
from pydantic import Extra
from langchain.embeddings import CacheBackedEmbeddings
from langchain_community.vectorstores import FAISS
# from langchain.storage import LocalFileStore
from .customs.compressed_embeddings_cache import LocalFileStore
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.embeddings import HuggingFaceInstructEmbeddings
from langchain_community.embeddings import SentenceTransformerEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain.docstore.document import Document
import litellm

from .misc import cache_dir, get_tkn_length
from .logger import whi, red
from .typechecker import optional_typecheck
from .flags import is_verbose
from .env import WDOC_EXPIRE_CACHE_DAYS

def status(message: str):
    if is_verbose:
        whi(f"STATUS: {message}")

NB_LOADER_WORKERS = 10
NB_SAVER_WORKERS = 10

(cache_dir / "faiss_embeddings").mkdir(exist_ok=True)

# Source: https://api.python.langchain.com/en/latest/_modules/langchain_community/embeddings/huggingface.html#HuggingFaceEmbeddings
DEFAULT_EMBED_INSTRUCTION = "Represent the document for retrieval: "
DEFAULT_QUERY_INSTRUCTION = "Represent the question for retrieving supporting documents: "


@optional_typecheck
def iter_merge(db1: FAISS, db2: FAISS) -> List[Document]:
    """
    merge inplace db1 by adding it each document and embeddings of db2.
    """
    failed = []
    doc_ids = list(db2.docstore._dict.keys())
    # get the embedding of each document
    vecs = faiss.rev_swig_ptr(
        db2.index.get_xb(),
        len(doc_ids) * db2.index.d
    ).reshape(len(doc_ids), db2.index.d)
    vecs = np.vsplit(vecs, vecs.shape[0])
    vecs = [v.squeeze() for v in vecs]
    for docuid, embe in zip(doc_ids, vecs):
        docu = db2.docstore._dict[docuid]
        try:
            db1.add_embeddings(
                text_embeddings=[(docu.page_content, embe)],
                metadatas=[docu.metadata],
                ids=[docuid],
            )
        except ValueError as err:
            if "Tried to add ids that already exist" not in str(err):
                raise
            failed.append(docu)
    return failed

def score_function(distance: float) -> float:
    """
    Scoring function for faiss to make sure it's positive.

    Related issue: https://github.com/langchain-ai/langchain/issues/17333
    """
    # if distance < 0:
    #     red(f"Distance was under 0: {distance}")
    #     distance = 0
    # elif distance > 1:
    #     red(f"Distance was above 1: {distance}")
    #     distance = 1
    # assert distance >= 0 and distance <= 1, f"Invalid distance value: {distance}"
    return (1 - distance) ** 2

@optional_typecheck
def faiss_hotfix(vectorstore: FAISS) -> FAISS:
    """
    Wrap around FAISS's vector search to hot fix things:

    - check if the found IDs are indeed in the database. For some reason
    FAISS in some cases ends up returning ids that do not match its own
    id index so it crashes.

    """

    @optional_typecheck
    def filter_ids(func: Callable, get_mask: Callable) -> Callable:
        @wraps(func)
        def wrapper(vector, k):
            original_scores, original_ids = func(vector, k)

            new_ids = original_ids.squeeze()[get_mask(original_ids.squeeze())]

            diff = k - new_ids.shape[0]
            assert diff >= 0, f"Asked for {k} vectors but go more: {new_ids.shape}"
            if diff == 0:
                assert original_scores.shape == original_ids.shape
                assert original_ids.squeeze().shape == new_ids.shape
                return original_scores, original_ids
            else:
                trial = 0
                while diff > 0 and trial < 10:
                    trial += 1

                    trial_scores, trial_ids = func(vector, k + diff)
                    mask = get_mask(trial_ids.squeeze())
                    trial_ids = trial_ids.squeeze()[mask]

                    diff = k - trial_ids.shape[0]
                    assert diff >= 0, f"Asked at trial {trial} for {k} vectors but go more: {new_ids.shape}"

                trial_scores = trial_scores.squeeze()[mask].reshape(1, -1)
                trial_ids = trial_ids.reshape(1, -1)

                assert trial_scores.shape == trial_ids.shape
                return trial_scores, trial_ids
        return wrapper

    ok_ids = np.array(list(vectorstore.index_to_docstore_id.keys())).squeeze()
    vectorstore.index.search = filter_ids(
        func=vectorstore.index.search,
        get_mask=np.vectorize(lambda ar: ar in ok_ids),
    )
    return vectorstore

@optional_typecheck
def load_embeddings(
    embed_model: str,
    embed_kwargs: dict,
    load_embeds_from: Optional[Union[str, PosixPath]],
    save_embeds_as: Union[str, PosixPath],
    loaded_docs: Any,
    dollar_limit: Union[int, float],
    private: bool,
    use_rolling: bool,
    cli_kwargs: dict,
) -> Tuple[FAISS, CacheBackedEmbeddings]:
    """loads embeddings for each document"""
    backend = embed_model.split("/", 1)[0]
    embed_model = embed_model.replace(backend + "/", "")
    embed_model_str = embed_model.replace("/", "_")
    if "embed_instruct" in cli_kwargs and cli_kwargs["embed_instruct"]:
        instruct = True
    else:
        instruct = False

    if is_verbose:
        whi(f"Selected embedding model '{embed_model}' of backend {backend}")
    if backend == "openai":
        assert not private, f"Set private but tried to use openai embeddings"
        assert "OPENAI_API_KEY" in os.environ and os.environ[
            "OPENAI_API_KEY"] and "REDACTED" not in os.environ["OPENAI_API_KEY"], "Missing OPENAI_API_KEY"

        embeddings = OpenAIEmbeddings(
            model=embed_model,
            # model="text-embedding-ada-002",
            openai_api_key=os.environ["OPENAI_API_KEY"],
            **embed_kwargs,
        )

    elif backend == "huggingface":
        assert not private, f"Set private but tried to use huggingface embeddings, which might not be as private as using sentencetransformers"
        model_kwargs = {
            "device": "cpu",
            # "device": "cuda",
        }
        model_kwargs.update(embed_kwargs)
        if "google" in embed_model and "gemma" in embed_model.lower():
            assert "HUGGINGFACE_API_KEY" in os.environ and os.environ[
                "HUGGINGFACE_API_KEY"] and "REDACTED" not in os.environ["HUGGINGFACE_API_KEY"], "Missing HUGGINGFACE_API_KEY"
            hftkn = os.environ["HUGGINGFACE_API_KEY"]
            # your token to use the models
            model_kwargs['use_auth_token'] = hftkn
        if instruct:
            embeddings = HuggingFaceInstructEmbeddings(
                model_name=embed_model,
                model_kwargs=model_kwargs,
                embed_instruction=DEFAULT_EMBED_INSTRUCTION,
                query_instruction=DEFAULT_QUERY_INSTRUCTION,
            )
        else:
            embeddings = HuggingFaceEmbeddings(
                model_name=embed_model,
                model_kwargs=model_kwargs,
            )

        if "google" in embed_model and "gemma" in embed_model.lower():
            # please select a token to use as `pad_token` `(tokenizer.pad_token = tokenizer.eos_token e.g.)`
            # or add a new pad token via `tokenizer.add_special_tokens({'pad_token': '[pad]'})
            embeddings.client.tokenizer.pad_token = embeddings.client.tokenizer.eos_token

    elif backend == "sentencetransformers":
        if private:
            red(f"Private is set and will use sentencetransformers backend")
        if use_rolling:
            embed_kwargs.update(
                {
                    "batch_size": 1,
                    "pooling": "meanpool",
                    "device": None,
                }
            )
            embeddings = RollingWindowEmbeddings(
                model_name=embed_model,
                encode_kwargs=embed_kwargs,
            )
        else:
            embed_kwargs.update(
                {
                    "batch_size": 1,
                    "device": None,
                }
            )
            embeddings = SentenceTransformerEmbeddings(
                model_name=embed_model,
                encode_kwargs=embed_kwargs,
            )

    else:
        raise ValueError(f"Invalid embedding backend: {backend}")

    if "/" in embed_model:
        try:
            if Path(embed_model).exists():
                with open(Path(embed_model).resolve().absolute().__str__(), "rb") as f:
                    h = hashlib.sha256(
                        f.read() + str(instruct)
                    ).hexdigest()[:15]
                embed_model_str = Path(embed_model).name + "_" + h
        except Exception:
            pass
    assert "/" not in embed_model_str
    if private:
        embed_model_str = "private_" + embed_model_str

    lfs = LocalFileStore(
        root_path=cache_dir / "CacheEmbeddings" / embed_model_str,
        update_atime=True,
        compress=True
    )
    cache_content = list(lfs.yield_keys())
    whi(f"Found {len(cache_content)} embeddings in local cache")

    # cached_embeddings = embeddings
    cached_embeddings = CacheBackedEmbeddings.from_bytes_store(
        embeddings,
        lfs,
        namespace=embed_model_str,
    )

    # reload passed embeddings
    if load_embeds_from:
        red("Reloading documents and embeddings from file")
        path = Path(load_embeds_from)
        assert path.exists(), f"file not found at '{path}'"
        db = FAISS.load_local(str(path), cached_embeddings,
                              allow_dangerous_deserialization=True)
        n_doc = len(db.index_to_docstore_id.keys())
        red(f"Loaded {n_doc} documents")
        return faiss_hotfix(db), cached_embeddings

    whi("\nLoading embeddings.")

    docs = loaded_docs
    if len(docs) >= 50:
        docs = sorted(docs, key=lambda x: random.random())

    embeddings_cache = cache_dir / "faiss_doc_indexes" / embed_model_str
    embeddings_cache.mkdir(exist_ok=True, parents=True)
    ti = time.time()
    whi(f"Creating FAISS index for {len(docs)} documents")

    in_cache = [p for p in embeddings_cache.iterdir()]
    whi(f"Found {len(in_cache)} embeddings in cache")
    to_embed = []

    # load previous faiss index from cache
    loader_queues = [(queue.Queue(maxsize=10), queue.Queue())
                     for i in range(NB_LOADER_WORKERS)]
    loader_workers = [
        threading.Thread(
            target=faiss_loader,
            args=(cached_embeddings, qin, qout),
            daemon=False,
        ) for qin, qout in loader_queues]
    [t.start() for t in loader_workers]
    timeout = 10
    list_of_files = set((
        f.stem
        for f in embeddings_cache.iterdir()
        if "faiss_index" in f.suffix
    ))
    for doc in tqdm(docs, desc="Loading embeddings from cache"):
        if doc.metadata["content_hash"] in list_of_files:
            fi = embeddings_cache / \
                str(doc.metadata["content_hash"] + ".faiss_index")
            assert fi.exists(), f"fi does not exist: {fi}"
            # select 2 workers at random and choose the one with the smallest queue
            queue_candidates = random.sample(loader_queues, k=2)
            queue_sizes = [q[0].qsize() for q in queue_candidates]
            ind = queue_sizes.index(min(queue_sizes))
            lq = queue_candidates[ind][0]
            assert loader_workers[ind].is_alive(), f"Loader worker #{ind} is dead"
            lq.put((fi, doc.metadata))
        else:
            to_embed.append(doc)

    # ask workers to stop and return their db then get the merged dbs
    whi("Asking loader workers to shutdown")
    whi("Putting stop order in the queue")
    [q[0].put((False, None)) for q in loader_queues]
    whi("Waiting for answers")
    merged_dbs = []
    for iq, q in enumerate(loader_queues):
        assert loader_workers[iq].is_alive(), f"Loader worker #{ind} is dead"
        while True:
            try:
                whi(f"Waiting for partial db from loader worker #{iq}")
                val = q[1].get(timeout=timeout)
                whi("Got it")
                merged_dbs.append(val)
                break
            except queue.Empty:
                red(f"Thread #{iq} failed to reply. Retrying. Its input queue size is {q[0].qsize()}")

    start_stopping_threads = time.time()
    while any(t.is_alive() for t in loader_workers):
        if time.time() - start_stopping_threads > 10 * 60:
            red(
                f"Waited for threads to stop for "
                f"{time.time()-start_stopping_threads:.4f}s so continuing "
                "but do report this because something seems to have gone wrong."
            )
            break
        for ith, t in enumerate(loader_workers):
            if t.is_alive():
                t.join(timeout=timeout)
                if t.is_alive():
                    q = loader_queues[ith]
                    qsize = q.qsize()
                    red(
                        f"Thread #{ith+1}/{len(loader_workers)} is still "
                        f"running with queue size of {qsize}"
                    )
    if any([t.is_alive() for t in loader_workers]):
        red(f"Some faiss loader workers failed to stop: {len([t for t in loader_workers if t.is_alive()])}/{len(loader_workers)}")
    out_vals = [q[1].get(timeout=1) for q in loader_queues]
    if not all(val == "Stopped" for val in out_vals):
        red("Unexpected output of some loader queues: \n* " + "\n* ".join(out_vals))

    # merge dbs as one
    db = None
    if merged_dbs:
        assert db is None
        db = merged_dbs.pop(0)
    failed_to_merge = []
    if merged_dbs:
        for m in merged_dbs:
            try:
                db.merge_from(m)
            except ValueError as err:
                if "Tried to add ids that already exist" not in str(err):
                    raise
                failed_to_merge.extend(iter_merge(db, m))

        in_db = len(db.docstore._dict.keys())
        if in_db != len(docs) - len(to_embed) - len(failed_to_merge):
            red(
                f"Invalid number of loaded documents: found {in_db} but "
                f"expected {len(docs)-len(to_embed)-len(failed_to_merge)}"
            )

    whi(f"Docs left to embed: {len(to_embed)}")

    # remove the cached embeddings that are too old
    if WDOC_EXPIRE_CACHE_DAYS:
        cached_path=cache_dir / "CacheEmbeddings" / embed_model_str
        if not cached_path.exists():
            cached_path.mkdir(parents=True)
        current_time = time.time()
        for dir_to_expire in [cached_path, embeddings_cache]:
            n_total = 0
            n_cleaned = 0
            space_retrieved = 0
            for file in dir_to_expire.iterdir():
                last_access_time = file.stat().st_atime
                days_since_last_access = (current_time - last_access_time) / (24 * 3600)
                n_total += 1
                if days_since_last_access >= WDOC_EXPIRE_CACHE_DAYS:
                    n_cleaned += 1
                    if file.is_dir():
                        space_retrieved += sum(f.stat().st_size for f in file.rglob('*') if f.is_file())
                    elif file.is_file():
                        space_retrieved += file.stat().st_size
                    file.unlink(missing_ok=False)
            whi(
                f"Number of files removed from {dir_to_expire.name} cache: "
                f"{n_cleaned}/{n_total} ({space_retrieved / 1024 / 1024:.3f}Mb)"
            )

    # check price of embedding
    full_tkn = sum([get_tkn_length(doc.page_content) for doc in to_embed])
    whi(
        f"Total number of tokens in documents (not checking if already present in cache): '{full_tkn}'")
    if private:
        whi("Not checking token price because private is set")
        price = 0
    elif backend != "openai":
        whi(
            f"Not checking token price because using a private backend: {backend}")
        price = 0
    elif f"{backend}/{embed_model}" in litellm.model_cost:
        price = litellm.model_cost[f"{backend}/{embed_model}"]["input_cost_per_token"]
        assert litellm.model_cost[f"{backend}/{embed_model}"]["output_cost_per_token"] == 0
    elif embed_model in litellm.model_cost:
        price = litellm.model_cost[embed_model]["input_cost_per_token"]
        assert litellm.model_cost[embed_model]["output_cost_per_token"] == 0
    else:
        raise Exception(
            red(f"Couldn't find the price of embedding model {embed_model}"))

    dol_price = full_tkn * price
    red(f"Total cost to embed all tokens is ${dol_price:.6f}")
    if dol_price > dollar_limit:
        ans = input("Do you confirm you are okay to pay this? (y/n)\n>")
        if ans.lower() not in ["y", "yes"]:
            red("Quitting.")
            raise SystemExit()

    # create a faiss index for batch of documents
    if to_embed:
        ts = time.time()
        batch_size = 1000
        batches = [
            [i * batch_size, (i + 1) * batch_size]
            for i in range(len(to_embed) // batch_size + 1)
        ]
        saver_queues = [(queue.Queue(maxsize=10), queue.Queue())
                        for i in range(NB_SAVER_WORKERS)]
        saver_workers = [
            threading.Thread(
                target=faiss_saver,
                args=(embeddings_cache, cached_embeddings, qin, qout),
                daemon=False,
            ) for qin, qout in saver_queues]
        [t.start() for t in saver_workers]
        assert all([t.is_alive() for t in saver_workers]
                   ), "Saver workers failed to load"

        def embedandsave_one_batch(
            batch: List,
            ib: int,
            saver_queues: List[Tuple[queue.Queue, queue.Queue]] = saver_queues,
        ):
            whi(f"Embedding batch #{ib + 1}")
            temp = FAISS.from_documents(
                to_embed[batch[0]:batch[1]],
                cached_embeddings,
                normalize_L2=True,
                relevance_score_fn=score_function,
            )

            whi(f"Saving batch #{ib + 1}")
            # save the faiss index as 1 embedding for 1 document
            # get the id of each document
            doc_ids = list(temp.docstore._dict.keys())
            # get the embedding of each document
            vecs = faiss.rev_swig_ptr(temp.index.get_xb(), len(
                doc_ids) * temp.index.d).reshape(len(doc_ids), temp.index.d)
            vecs = np.vsplit(vecs, vecs.shape[0])
            vecs = [v.squeeze() for v in vecs]
            for docuid, embe in zip(doc_ids, vecs):
                docu = temp.docstore._dict[docuid]
                assert all([t.is_alive()
                           for t in saver_workers]), "Some saving thread died"

                # select 2 workers at random and choose the one with the smallest queue
                queue_candidates = random.sample(saver_queues, k=2)
                queue_sizes = [q[0].qsize() for q in queue_candidates]
                ind = queue_sizes.index(min(queue_sizes))
                sq = queue_candidates[ind][0]
                assert saver_workers[ind].is_alive(), f"Worker #{ind} is dead"
                sq.put((True, docuid, docu, embe))

            return temp
        temp_dbs = Parallel(
            backend="threading",
            n_jobs=5,
            verbose=0 if not is_verbose else 51,
        )(
            delayed(embedandsave_one_batch)(
                batch=batch,
                ib=ib,
            )
            for ib, batch in tqdm(
                enumerate(batches),
                total=len(batches),
                desc="Embedding by batch",
                # disable=not is_verbose,
            )
        )
        failed_to_merge = []
        for temp in temp_dbs:
            if not db:
                db = temp
            else:
                try:
                    db.merge_from(temp)
                except ValueError as err:
                    if "Tried to add ids that already exist" not in str(err):
                        raise
                    failed_to_merge.extend(iter_merge(db, temp))
        if failed_to_merge:
            red(f"Failed to merge {len(failed_to_merge)} documents after embeddings")

        whi("Waiting for saver workers to finish.")

        [q[0].put((False, None, None, None)) for i, q in enumerate(
                saver_queues) if saver_workers[i].is_alive()]
        start_stopping_threads = time.time()
        while any(t.is_alive() for t in saver_workers):
            if time.time() - start_stopping_threads > 10 * 60:
                red(
                    f"Waited for threads to stop for "
                    f"{time.time()-start_stopping_threads:.4f}s so continuing "
                    "but do report this because something seems to have gone wrong."
                )
                break
            for ith, t in enumerate(saver_workers):
                if t.is_alive():
                    t.join(timeout=timeout)
                    if t.is_alive():
                        q = saver_queues[ith]
                        qsize = q.qsize()
                        red(
                            f"Thread #{ith+1}/{len(saver_workers)} is still "
                            f"running with queue size of {qsize}"
                        )
        if any([t.is_alive() for t in saver_workers]):
            red(f"Some faiss saver workers failed to stop: {len([t for t in saver_workers if t.is_alive()])}/{len(saver_workers)}")
        out_vals = [q[1].get(timeout=timeout) for q in saver_queues]
        if not all(val == "Stopped" for val in out_vals):
            red("Unexpected output of some saver queues: \n* " + "\n* ".join(out_vals))

        whi(f"Saving indexes took {time.time()-ts:.2f}s")

    whi(f"Done creating index (total time: {time.time()-ti:.2f}s)")

    # saving embeddings
    db.save_local(save_embeds_as)

    return faiss_hotfix(db), cached_embeddings


@optional_typecheck
def faiss_loader(
        cached_embeddings: CacheBackedEmbeddings,
        qin: queue.Queue,
        qout: queue.Queue) -> None:
    """load a faiss index. Merge many other index to it. Then return the
    merged index. This makes it way fast to load a very large number of index
    """
    db = None
    while True:
        fi, metadata = qin.get()
        if fi is False:
            assert metadata is None
            qout.put(db)
            qout.put("Stopped")
            return
        assert metadata is not None

        temp = FAISS.load_local(
            fi,
            cached_embeddings,
            allow_dangerous_deserialization=True,
            relevance_score_fn=score_function,
        )

        ids_list = list(temp.docstore._dict.keys())
        assert len(ids_list) == 1

        if not db:
            db = temp
            continue

        did = ids_list[0]
        if did in db.docstore._dict.keys():
            red(f"Not thread-loading doc as already present: {did}")
            continue
        temp.docstore._dict[did].metadata = metadata
        try:
            db.merge_from(temp)
        except ValueError as err:
            red(f"Error when loading cache from {fi}: {err}\nDeleting {fi}")
            [p.unlink() for p in fi.iterdir()]
            fi.rmdir()


@optional_typecheck
def faiss_saver(
        path: Union[str, PosixPath],
        cached_embeddings: CacheBackedEmbeddings,
        qin: queue.Queue,
        qout: queue.Queue) -> None:
    """create a faiss index containing only a single document then save it"""
    while True:
        message, docid, document, embedding = qin.get()
        if message is False:
            assert docid is None and document is None and embedding is None
            qout.put("Stopped")
            return

        file = (path / str(document.metadata["content_hash"] + ".faiss_index"))
        db = FAISS.from_embeddings(
            text_embeddings=[[document.page_content, embedding]],
            embedding=cached_embeddings,
            metadatas=[document.metadata],
            ids=[docid],
            normalize_L2=True,
            relevance_score_fn=score_function,
        )
        db.save_local(file)


class RollingWindowEmbeddings(SentenceTransformerEmbeddings, extra=Extra.allow):
    @optional_typecheck
    def __init__(self, *args, **kwargs):
        assert "encode_kwargs" in kwargs
        if "normalize_embeddings" in kwargs["encode_kwargs"]:
            assert kwargs["encode_kwargs"]["normalize_embeddings"] is False, (
                "Not supposed to normalize embeddings using RollingWindowEmbeddings")
        assert kwargs["encode_kwargs"]["pooling"] in ["maxpool", "meanpool"]
        pooltech = kwargs["encode_kwargs"]["pooling"]
        del kwargs["encode_kwargs"]["pooling"]

        super().__init__(*args, **kwargs)
        self.__pool_technique = pooltech

    @optional_typecheck
    def embed_documents(self, texts, *args, **kwargs):
        """sbert silently crops any token above the max_seq_length,
        so we do a windowing embedding then pool (maxpool or meanpool)
        No normalization is done because the faiss index does it for us
        """
        model = self.client
        sentences = texts
        max_len = model.get_max_seq_length()

        if not isinstance(max_len, int):
            # the clip model has a different way to use the encoder
            # sources : https://github.com/UKPLab/sentence-transformers/issues/1269
            assert "clip" in str(model).lower(), (
                f"sbert model with no 'max_seq_length' attribute and not clip: '{model}'")
            max_len = 77
            encode = model._first_module().processor.tokenizer.encode
        else:
            if hasattr(model.tokenizer, "encode"):
                # most models
                encode = model.tokenizer.encode
            else:
                # word embeddings models like glove
                encode = model.tokenizer.tokenize

        assert isinstance(max_len, int), "n must be int"
        n23 = (max_len * 2) // 3
        add_sent = []  # additional sentences
        add_sent_idx = []  # indices to keep track of sub sentences

        for i, s in enumerate(sentences):
            # skip if the sentence is short
            length = len(encode(s))
            if length <= max_len:
                continue

            # otherwise, split the sentence at regular interval
            # then do the embedding of each
            # and finally pool those sub embeddings together
            sub_sentences = []
            words = s.split(" ")
            avg_tkn = length / len(words)
            # start at 90% of the supposed max_len
            j = int(max_len / avg_tkn * 0.8)
            while len(encode(" ".join(words))) > max_len:

                # if reached max length, use that minus one word
                until_j = len(encode(" ".join(words[:j])))
                if until_j >= max_len:
                    jjj = 1
                    while len(encode(" ".join(words[:j-jjj]))) >= max_len:
                        jjj += 1
                    sub_sentences.append(" ".join(words[:j-jjj]))

                    # remove first word until 1/3 of the max_token was removed
                    # this way we have a rolling window
                    jj = max(1, int((max_len // 3) / avg_tkn * 0.8))
                    while len(encode(" ".join(words[jj:j-jjj]))) > n23:
                        jj += 1
                    words = words[jj:]

                    j = int(max_len / avg_tkn * 0.8)
                else:
                    diff = abs(max_len - until_j)
                    if diff > 10:
                        j += max(1, int(10 / avg_tkn))
                    else:
                        j += 1

            sub_sentences.append(" ".join(words))

            sentences[i] = " "  # discard this sentence as we will keep only
            # the sub sentences pooled

            # remove empty text just in case
            if "" in sub_sentences:
                while "" in sub_sentences:
                    sub_sentences.remove("")
            assert sum([len(encode(ss)) > max_len for ss in sub_sentences]) == 0, (
                f"error when splitting long sentences: {sub_sentences}")
            add_sent.extend(sub_sentences)
            add_sent_idx.extend([i] * len(sub_sentences))

        if add_sent:
            sent_check = [
                len(encode(s)) > max_len
                for s in sentences
            ]
            addsent_check = [
                len(encode(s)) > max_len
                for s in add_sent
            ]
            assert sum(sent_check + addsent_check) == 0, (
                f"The rolling average failed apparently:\n{sent_check}\n{addsent_check}")

        vectors = super().embed_documents(sentences + add_sent)
        t = type(vectors)

        if isinstance(vectors, list):
            vectors = np.array(vectors)

        if add_sent:
            # at the position of the original sentence (not split)
            # add the vectors of the corresponding sub_sentence
            # then return only the 'pooled' section
            assert len(add_sent) == len(add_sent_idx), (
                "Invalid add_sent length")
            offset = len(sentences)
            for sid in list(set(add_sent_idx)):
                id_range = [i for i, j in enumerate(add_sent_idx) if j == sid]
                add_sent_vec = vectors[
                    offset + min(id_range): offset + max(id_range), :]
                if self.__pool_technique == "maxpool":
                    vectors[sid] = np.amax(add_sent_vec, axis=0)
                elif self.__pool_technique == "meanpool":
                    vectors[sid] = np.sum(add_sent_vec, axis=0)
                else:
                    raise ValueError(self.__pool_technique)
            vectors = vectors[:offset]

        if not isinstance(vectors, t):
            vectors = vectors.tolist()
        assert isinstance(vectors, t), "wrong type?"
        return vectors

