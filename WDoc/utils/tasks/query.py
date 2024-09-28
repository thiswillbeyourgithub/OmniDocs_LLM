"""
Chain (logic) used to query a document.
"""

import re
from typing import Tuple, List, Union, Literal
from numpy.typing import NDArray

from langchain.docstore.document import Document
from langchain_core.runnables import chain
from langchain_core.runnables.base import RunnableLambda
from tqdm import tqdm
import numpy as np

from langchain.embeddings import CacheBackedEmbeddings
from langchain_community.chat_models.fake import FakeListChatModel
from langchain_community.chat_models import ChatLiteLLM
from langchain_openai import ChatOpenAI
import pandas as pd
import sklearn.metrics as metrics
import sklearn.decomposition as decomposition
import sklearn.preprocessing as preprocessing
import scipy

from ..typechecker import optional_typecheck
from ..errors import NoDocumentsRetrieved, NoDocumentsAfterLLMEvalFiltering, InvalidDocEvaluationByLLMEval
from ..logger import red, whi
from ..misc import thinking_answer_parser, get_tkn_length
from ..flags import is_verbose

irrelevant_regex = re.compile(r"\bIRRELEVANT\b")


@optional_typecheck
def check_intermediate_answer(ans: str) -> bool:
    "filters out the intermediate answers that are deemed irrelevant."
    if "<answer>IRRELEVANT</answer>" in ans:
        return False
    if (
        ((not irrelevant_regex.search(ans)) and len(ans) < len("IRRELEVANT") * 2)
        or
        len(ans) >= len("IRRELEVANT") * 2
    ):
        return True
    return False


@chain
@optional_typecheck
def refilter_docs(inputs: dict) -> List[Document]:
    "filter documents find via RAG based on the digit answered by the eval llm"
    unfiltered_docs = inputs["unfiltered_docs"]
    evaluations = inputs["evaluations"]
    assert isinstance(
        unfiltered_docs, list), f"unfiltered_docs should be a list, not {type(unfiltered_docs)}"
    assert isinstance(
        evaluations, list), f"evaluations should be a list, not {type(evaluations)}"
    assert len(unfiltered_docs) == len(
        evaluations), f"len of unfiltered_docs is {len(unfiltered_docs)} but len of evaluations is {len(evaluations)}"
    if not unfiltered_docs:
        raise NoDocumentsRetrieved("No document corresponding to the query")

    filtered_docs = []
    for ie, evals in enumerate(evaluations):  # iterating over each document
        if not isinstance(evals, list):
            evals = [evals]
        answers = [thinking_answer_parser(ev)["answer"] for ev in evals]
        for ia, a in enumerate(answers):
            try:
                a = int(a)
            except Exception as err:
                red(f"Document was not evaluated with a number: '{err}' for answer '{a}'\nKeeping the document anyway.")
                a = 5
            answers[ia] = a

        if sum(answers) != 0:
            filtered_docs.append(unfiltered_docs[ie])

    if not filtered_docs:
        raise NoDocumentsAfterLLMEvalFiltering(
            "No document remained after filtering with the query")
    return filtered_docs


@optional_typecheck
def parse_eval_output(output: str) -> str:
    mess = f"The eval LLM returned an output that can't be parsed as expected: '{output}'"
    # empty
    if not output.strip():
        raise InvalidDocEvaluationByLLMEval(mess)

    parsed = thinking_answer_parser(output)

    if is_verbose:
        whi(f"Eval LLM output: '{output}'")

    answer = parsed["answer"]
    try:
        answer = int(answer)
        return str(answer)
    except Exception as err:
        red(f"Document was not evaluated with a number: '{err}' for answer '{answer}'\nKeeping the document anyway.")
        return str(5)

    if "-" in parsed["answer"]:
        raise InvalidDocEvaluationByLLMEval(mess)
    digits = [d for d in list(parsed["answer"]) if d.isdigit()]

    # contain no digits
    if not digits:
        raise InvalidDocEvaluationByLLMEval(mess)

    # good
    elif len(digits) == 1:
        if digits[0] == "0":
            return "0"
        elif digits[0] == "1":
            return "1"
        elif digits[0] == "2":
            return "1"
        else:
            raise InvalidDocEvaluationByLLMEval(mess)
    else:
        # ambiguous
        raise InvalidDocEvaluationByLLMEval(mess)



@optional_typecheck
def collate_intermediate_answers(
    list_ia: List[str],
    embedding_engine: CacheBackedEmbeddings,
    ) -> str:
    """write the intermediate answers in a single string to be
    combined by the LLM"""
    # remove answers deemed irrelevant
    list_ia = [ia for ia in list_ia if check_intermediate_answer(ia)]
    assert len(list_ia) >= 2, f"Cannot collate a single intermediate answer!\n{list_ia[0]}"

    out = "Intermediate answers:"
    for iia, ia in enumerate(list_ia):
        out += f"""
<source_id>
{iia + 1}
</source_id>
<ia>
{ia}
</ia>\n""".lstrip()
    return out

@optional_typecheck
def semantic_batching(
    texts: List[str],
    embedding_engine: CacheBackedEmbeddings,
    ) -> List[List[str]]:
    """
    Given a list of text, embed them, do a hierarchical clutering then
    sort the list according to the leaf order, then create buckets that best
    contain each subtopic while keeping a reasonnable number of tokens.
    This probably helps the LLM to combine the intermediate answers
    into one.
    Returns directly if less than 5 texts.
    """
    assert texts, "No input text received"

    # deduplicate texts
    temp = []
    [temp.append(t) for t in texts if t not in temp]
    texts = temp

    if len(texts) < 5:
        return [texts]

    # get embeddings
    embeds = np.array([embedding_engine.embed_query(t) for t in texts]).squeeze()
    n_dim = embeds.shape[1]
    assert n_dim > 2, f"Unexpected number of dimension: {n_dim}, shape was {embeds.shape}"

    max_n_dim = min(100, len(texts))

    # optional dimension reduction to gain time
    if n_dim > max_n_dim:
        scaler = preprocessing.StandardScaler()
        embed_scaled = scaler.fit_transform(embeds)
        pca = decomposition.PCA(n_components=max_n_dim)
        embeds_reduced = pca.fit_transform(embed_scaled)
        assert embeds_reduced.shape[0] == embeds.shape[0]
        vr = np.cumsum(pca.explained_variance_ratio_)[-1]
        if vr <= 0.95:
            red(f"Found lower than exepcted PCA explained variance ratio: {vr:.4f}")
        embeddings = pd.DataFrame(
            columns=[f"v_{i}" for i in range(embeds_reduced.shape[1])],
            index=[i for i in range(len(texts))],
            data=embeds_reduced,
        )
    else:
        embeddings = pd.DataFrame(
            columns=[f"v_{i}" for i in range(embeds.shape[1])],
            index=[i for i in range(len(texts))],
            data=embeds,
        )

    # get the pairwise distance matrix
    pairwise_distances = metrics.pairwise_distances
    pd_dist = pd.DataFrame(
        columns=embeddings.index,
        index=embeddings.index,
        data=pairwise_distances(
            embeddings.values,
            n_jobs=-1,
            metric="euclidean",
            )
        )
    # make sure the intersection is 0 and not a very small float
    for ind in pd_dist.index:
        pd_dist.at[ind, ind] = 0
    # make sure it's symetric
    pd_dist = pd_dist.add(pd_dist.T).div(2)

    # get the hierarchichal semantic sorting order
    dist: NDArray[int] = scipy.spatial.distance.squareform(pd_dist.values)  # convert to condensed format
    Z: NDArray[Tuple[int, Literal[4]]] = scipy.cluster.hierarchy.linkage(
        dist,
        method='ward',
        optimal_ordering=True
    )

    order: NDArray[int] = scipy.cluster.hierarchy.leaves_list(Z)

    # # this would just return the list of strings in the best order
    # out_texts = [texts[o] for o in order]
    # assert len(set(out_texts)) == len(out_texts), "duplicates"
    # assert len(out_texts) == len(texts), "extra out_texts"
    # assert not any(o for o in out_texts if o not in texts)
    # assert not any(t for t in texts if t not in out_texts)
    # # whi(f"Done in {int(time.time()-start)}s")
    # assert len(texts) == len(out_texts)

    # get each bucket if we were only looking at the number of texts
    for divider in [2, 3, 4, 5]:
        cluster_labels = scipy.cluster.hierarchy.fcluster(
            Z,
            len(pd_dist.index)//divider,
            criterion='maxclust'
        )
        labels = np.unique(cluster_labels)
        labels.sort()
        if len(labels) != 1:  # re cluster if only one label found
            break
    assert len(labels) > 1, cluster_labels

    # make sure no cluster contains only one text
    for lab in labels:
        if (cluster_labels == lab).sum() == 1:
            t = texts[np.argmax(cluster_labels==lab)]
            t_closest = np.argmin(pd_dist.loc[texts.index(t), :])
            l_closest = cluster_labels[t_closest]
            if (cluster_labels == l_closest).sum() + 1 == len(texts):
                # merging small to big would result in only one cluster:
                # better to even them out
                assert len(labels) == 2, labels
                cluster_labels[texts.index(t_closest)] = lab
            else:  # good to go
                cluster_labels[cluster_labels==lab] = l_closest
    labels = np.unique(cluster_labels)
    labels.sort()
    assert len(labels) > 1, cluster_labels

    # Create buckets
    buckets = []
    current_bucket = []
    current_tokens = 0
    max_token = 500

    # fill each bucket until reaching max_token
    text_sizes = {t:get_tkn_length(t) for t in texts}
    for lab in labels:
        lab_mask = np.argwhere(cluster_labels==lab)
        assert len(lab_mask) > 1, f"{lab_mask}\n{cluster_labels}"
        for clustid in lab_mask:
            text = texts[int(clustid)]
            size = text_sizes[text]
            if current_tokens + size > max_token:
                buckets.append(current_bucket)
                current_bucket = [text]
                current_tokens = 0
            else:
                current_bucket.append(text)
                current_tokens += size

        buckets.append(current_bucket)
        current_bucket = []
        current_tokens = 0
    assert all(bucket for bucket in buckets), "Empty buckets"
    for bucket in buckets:
        assert sum(text_sizes[t] for t in bucket) <= max_token, bucket

    # sort each bucket based on the optimal order
    for ib, b in enumerate(buckets):
        buckets[ib] = sorted(b, key=lambda t: order[texts.index(t)])

    # now if any bucket contains only one text, that means it has too many
    # tokens itself, so we reequilibrate from the previous buckets
    for ib, b in enumerate(buckets):
        assert b
        if len(b) == 1:
            assert text_sizes[b[0]] > max_token, b[0]
            # figure out which bucket to merge with
            if ib == 0:  # first , merge with next
                next_id = ib + 1
            elif ib != len(buckets):  # not first nor last, take the neighbour with least minimal distance
                t_cur = b[0]
                prev = min([pd_dist.loc[t_cur, t] for t in buckets[ib-1]])
                next = min([pd_dist.loc[t_cur, t] for t in buckets[ib+1]])
                if prev < next:
                    next_id = ib - 1
                else:
                    next_id = ib + 1
            elif ib == len(buckets):  # last, take the penultimate
                next_id = ib - 1
            assert buckets[next_id], buckets[next_id]

            if len(buckets[next_id]) == 1:  # both texts are big, merge them anyway
                if next_id > ib:
                    buckets[next_id].insert(0, b.pop())
                else:
                    buckets[next_id].append(b.pop())
                assert not b, b
            else:
                # send text to the next bucket, at the correct position
                if next_id > ib:
                    b.append(buckets[next_id].pop(0))
                else:
                    b.append(buckets[next_id].pop(-1))
            assert id(b) == id(buckets[ib])

    buckets = [b for b in buckets if b]
    assert all(len(b) >= 2 for b in buckets), f"Invalid size of buckets: '{[len(b) for b in buckets]}'"
    unchained = []
    [unchained.extend(b) for b in buckets]
    assert len(unchained) == len(set(unchained)), "There were duplicate texts in buckets!"
    assert all(t in texts for t in unchained), "Some text of buckets were added!"

    return buckets


@optional_typecheck
def pbar_chain(
    llm: Union[ChatLiteLLM, ChatOpenAI, FakeListChatModel],
    len_func: str,
    **tqdm_kwargs,
    ) -> RunnableLambda:
    "create a chain that just sets a tqdm progress bar"

    @chain
    def actual_pbar_chain(
        inputs: Union[dict, List],
        llm: Union[ChatLiteLLM, ChatOpenAI, FakeListChatModel] = llm,
        ) -> Union[dict, List]:

        llm.callbacks[0].pbar.append(
            tqdm(
                total=eval(len_func),
                **tqdm_kwargs,
            )
        )
        if not llm.callbacks[0].pbar[-1].total:
            red(f"Empty total for pbar: {llm.callbacks[0].pbar[-1]}")

        return inputs

    return actual_pbar_chain

@optional_typecheck
def pbar_closer(
    llm: Union[ChatLiteLLM, ChatOpenAI, FakeListChatModel],
    ) -> RunnableLambda:
    "close a pbar created by pbar_chain"

    @chain
    def actual_pbar_closer(
        inputs: Union[dict, List],
        llm: Union[ChatLiteLLM, ChatOpenAI, FakeListChatModel] = llm,
        ) -> Union[dict, List]:
        pbar = llm.callbacks[0].pbar[-1]
        pbar.update(pbar.total - pbar.n)
        pbar.n = pbar.total
        pbar.close()

        return inputs
    return actual_pbar_closer
