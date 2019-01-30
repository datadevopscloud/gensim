"""`Online Non-Negative Matrix Factorization. <https://arxiv.org/abs/1604.02634>`

Implements online non-negative matrix factorization algorithm, which allows for fast latent topic inference.
This NMF implementation updates in a streaming fashion and works best with sparse corpora.

- W is a word-topic matrix
- h is a topic-document matrix
- r is a smoothed (v - Wh)
- v is an input word-document matrix

The idea of the algorithm is as follows:

.. code-block:: text

    Initialize W, A and B matrices

    Input corpus
    Split corpus to batches

    for v in batches:
        infer h (and optionally r):
            do coordinate gradient descent step to find h that minimizes (v - Wh) l2 norm
            bound h so that it is non-negative

            Not actually used because of a dreadfull performance hit
            # r = v - Wh
            # bound and smooth r

        update A and B

        update W:
            do gradient descent for W using A and B values

The NMF should be used whenever one needs faster topic extraction.

"""

import itertools

import logging
import numpy as np
import scipy.sparse
from gensim.models.nmf_pgd import solve_h
from scipy.stats import halfnorm

from gensim import interfaces
from gensim import matutils
from gensim import utils
from gensim.interfaces import TransformedCorpus
from gensim.models import basemodel, CoherenceModel

logger = logging.getLogger(__name__)

OLD_SCIPY = int(scipy.__version__.split('.')[1]) <= 18


class Nmf(interfaces.TransformationABC, basemodel.BaseTopicModel):
    """Online Non-Negative Matrix Factorization.

    `Renbo Zhao et al :"Online Nonnegative Matrix Factorization with Outliers" <https://arxiv.org/abs/1604.02634>`_

    """

    def __init__(
        self,
        corpus=None,
        num_topics=100,
        id2word=None,
        chunksize=2000,
        passes=1,
        lambda_=1.0,
        kappa=1.0,
        minimum_probability=0.01,
        use_r=False,
        w_max_iter=200,
        w_stop_condition=1e-4,
        h_r_max_iter=50,
        h_r_stop_condition=1e-3,
        eval_every=10,
        v_max=None,
        normalize=True,
        sparse_coef=3,
        random_state=None,
    ):
        r"""

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.
        num_topics : int, optional
            Number of topics to extract.
        id2word: :class:`~gensim.corpora.dictionary.Dictionary`, optional
            Mapping from token id to token. If not set words get replaced with word ids.
        chunksize: int, optional
            Number of documents to be used in each training chunk.
        passes: int, optional
            Number of full passes over the training corpus.
        \lambda_ : float, optional
            Deprecated.
        kappa : float, optional
            Gradient descent step size.
            Larger value makes the model train faster, but could lead to non-convergence if set too large.
        minimum_probability:
            If `normalize` is True, than only topics with larger probabilities than this are included in a result.
            If `normalize` is False, than only topics with larger factors than this are included in a result.
            If set to None, a value of 1e-8 is used to prevent 0s.
        use_r:
            Deprecated.
        w_max_iter: int, optional
            Maximum number of iterations to train W matrix per each batch.
        w_stop_condition: float, optional
            If error difference gets less than that, training of matrix ``W`` stops for current batch.
        h_r_max_iter: int, optional
            Maximum number of iterations to train h and r matrices per each batch.
        h_r_stop_condition: float
            If error difference gets less than that, training of matrices ``h`` and ``r`` stops for current batch.
        eval_every: int, optional
            Number of batches after which l2 norm of (v - Wh) is computed. Decreases performance if set too low.
        v_max: int, optional
            Deprecated.
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.
        sparse_coef: float, optional
            Deprecated.
        random_state: {np.random.RandomState, int}, optional
            Seed for random generator. Needed for reproducibility.

        """
        self.num_topics = num_topics
        self.id2word = id2word
        self.chunksize = chunksize
        self.passes = passes
        self._lambda_ = lambda_
        self._kappa = kappa
        self.minimum_probability = minimum_probability
        self.use_r = use_r
        self._w_max_iter = w_max_iter
        self._w_stop_condition = w_stop_condition
        self._h_r_max_iter = h_r_max_iter
        self._h_r_stop_condition = h_r_stop_condition
        self.eval_every = eval_every
        self.v_max = v_max
        self.normalize = normalize
        self.sparse_coef = sparse_coef
        self.random_state = utils.get_random_state(random_state)

        if self.id2word is None:
            self.id2word = utils.dict_from_corpus(corpus)

        self.num_tokens = len(self.id2word)

        self.A = None
        self.B = None

        self._W = None
        self.w_std = None
        self._w_error = None

        self._h = None
        self._r = None

        if corpus is not None:
            self.update(corpus)

    def get_topics(self, normalize=None):
        """Get the term-topic matrix learned during inference.

        Parameters
        ----------
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        numpy.ndarray
            The probability for each word in each topic, shape (`num_topics`, `vocabulary_size`).

        """
        dense_topics = self._W.T
        if normalize is None:
            normalize = self.normalize
        if normalize:
            return dense_topics / dense_topics.sum(axis=1).reshape(-1, 1)

        return dense_topics

    def __getitem__(self, bow, eps=None):
        return self.get_document_topics(bow, eps)

    def show_topics(self, num_topics=10, num_words=10, log=False, formatted=True, normalize=None):
        """Get the topics sorted by sparsity.

        Parameters
        ----------
        num_topics : int, optional
            Number of topics to be returned. Unlike LSA, there is no natural ordering between the topics in NMF.
            The returned topics subset of all topics is therefore arbitrary and may change between two NMF
            training runs.
        num_words : int, optional
            Number of words to be presented for each topic. These will be the most relevant words (assigned the highest
            probability for each topic).
        log : bool, optional
            Whether the result is also logged, besides being returned.
        formatted : bool, optional
            Whether the topic representations should be formatted as strings. If False, they are returned as
            2 tuples of (word, probability).
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        list of {str, tuple of (str, float)}
            a list of topics, each represented either as a string (when `formatted` == True) or word-probability
            pairs.

        """
        if normalize is None:
            normalize = self.normalize

        sparsity = self._W.mean(axis=0)

        if num_topics < 0 or num_topics >= self.num_topics:
            num_topics = self.num_topics
            chosen_topics = range(num_topics)
        else:
            num_topics = min(num_topics, self.num_topics)

            sorted_topics = list(matutils.argsort(sparsity))
            chosen_topics = (
                sorted_topics[: num_topics // 2] + sorted_topics[-num_topics // 2:]
            )

        shown = []

        topics = self.get_topics(normalize=normalize)

        for i in chosen_topics:
            topic = topics[i]
            bestn = matutils.argsort(topic, num_words, reverse=True).ravel()
            topic = [(self.id2word[id], topic[id]) for id in bestn]
            if formatted:
                topic = " + ".join(['%.3f*"%s"' % (v, k) for k, v in topic])

            shown.append((i, topic))
            if log:
                logger.info("topic #%i (%.3f): %s", i, sparsity[i], topic)

        return shown

    def show_topic(self, topicid, topn=10, normalize=None):
        """Get the representation for a single topic. Words here are the actual strings, in constrast to
        :meth:`~gensim.models.nmf.Nmf.get_topic_terms` that represents words by their vocabulary ID.

        Parameters
        ----------
        topicid : int
            The ID of the topic to be returned
        topn : int, optional
            Number of the most significant words that are associated with the topic.
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        list of (str, float)
            Word - probability pairs for the most relevant words generated by the topic.

        """
        if normalize is None:
            normalize = self.normalize

        return [
            (self.id2word[id], value)
            for id, value in self.get_topic_terms(topicid, topn,
                                                  normalize=normalize)
        ]

    def get_topic_terms(self, topicid, topn=10, normalize=None):
        """Get the representation for a single topic. Words the integer IDs, in constrast to
        :meth:`~gensim.models.nmf.Nmf.show_topic` that represents words by the actual strings.

        Parameters
        ----------
        topicid : int
            The ID of the topic to be returned
        topn : int, optional
            Number of the most significant words that are associated with the topic.
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        list of (int, float)
            Word ID - probability pairs for the most relevant words generated by the topic.

        """
        topic = self._W[:, topicid]

        if normalize is None:
            normalize = self.normalize
        if normalize:
            topic /= topic.sum()

        bestn = matutils.argsort(topic, topn, reverse=True)
        return [(idx, topic[idx]) for idx in bestn]

    def top_topics(self, corpus=None, texts=None, dictionary=None, window_size=None,
                   coherence='u_mass', topn=20, processes=-1):
        """Get the topics sorted by coherence.

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.
        texts : list of list of str, optional
            Tokenized texts, needed for coherence models that use sliding window based (i.e. coherence=`c_something`)
            probability estimator .
        dictionary : :class:`~gensim.corpora.dictionary.Dictionary`, optional
            Gensim dictionary mapping of id word to create corpus.
            If `model.id2word` is present, this is not needed. If both are provided, passed `dictionary` will be used.
        window_size : int, optional
            Is the size of the window to be used for coherence measures using boolean sliding window as their
            probability estimator. For 'u_mass' this doesn't matter.
            If None - the default window sizes are used which are: 'c_v' - 110, 'c_uci' - 10, 'c_npmi' - 10.
        coherence : {'u_mass', 'c_v', 'c_uci', 'c_npmi'}, optional
            Coherence measure to be used.
            Fastest method - 'u_mass', 'c_uci' also known as `c_pmi`.
            For 'u_mass' corpus should be provided, if texts is provided, it will be converted to corpus
            using the dictionary. For 'c_v', 'c_uci' and 'c_npmi' `texts` should be provided (`corpus` isn't needed)
        topn : int, optional
            Integer corresponding to the number of top words to be extracted from each topic.
        processes : int, optional
            Number of processes to use for probability estimation phase, any value less than 1 will be interpreted as
            num_cpus - 1.

        Returns
        -------
        list of (list of (int, str), float)
            Each element in the list is a pair of a topic representation and its coherence score. Topic representations
            are distributions of words, represented as a list of pairs of word IDs and their probabilities.

        """
        cm = CoherenceModel(
            model=self, corpus=corpus, texts=texts, dictionary=dictionary,
            window_size=window_size, coherence=coherence, topn=topn,
            processes=processes
        )
        coherence_scores = cm.get_coherence_per_topic()

        str_topics = []
        for topic in self.get_topics():  # topic = array of vocab_size floats, one per term
            bestn = matutils.argsort(topic, topn=topn, reverse=True)  # top terms for topic
            beststr = [(topic[_id], self.id2word[_id]) for _id in bestn]  # membership, token
            str_topics.append(beststr)  # list of topn (float membership, token) tuples

        scored_topics = zip(str_topics, coherence_scores)
        return sorted(scored_topics, key=lambda tup: tup[1], reverse=True)

    def log_perplexity(self, corpus):
        """Calculate perplexity bound on the specified corpus.

        Perplexity = e^(-bound).

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.

        Returns
        -------
        float
            The perplexity bound.

        """
        W = self.get_topics().T

        H = np.zeros((W.shape[1], len(corpus)))
        for bow_id, bow in enumerate(corpus):
            for topic_id, factor in self[bow]:
                H[topic_id, bow_id] = factor

        dense_corpus = matutils.corpus2dense(corpus, W.shape[0])

        pred_factors = W.dot(H)
        pred_factors /= pred_factors.sum(axis=0)

        return (np.log(pred_factors, where=pred_factors > 0) * dense_corpus).sum() / dense_corpus.sum()

    def get_term_topics(self, word_id, minimum_probability=None, normalize=None):
        """Get the most relevant topics to the given word.

        Parameters
        ----------
        word_id : int
            The word for which the topic distribution will be computed.
        minimum_probability : float, optional
            If `normalize` is True, than only topics with larger probabilities than this are included in a result.
            If `normalize` is False, than only topics with larger factors than this are included in a result.
            If set to None, a value of 1e-8 is used to prevent 0s.
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        list of (int, float)
            The relevant topics represented as pairs of their ID and their assigned probability, sorted
            by relevance to the given word.

        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability
        minimum_probability = max(minimum_probability, 1e-8)

        # if user enters word instead of id in vocab, change to get id
        if isinstance(word_id, str):
            word_id = self.id2word.doc2bow([word_id])[0][0]

        values = []

        word_topics = self._W[word_id]

        if normalize is None:
            normalize = self.normalize
        if normalize and word_topics.sum() > 0:
            word_topics /= word_topics.sum()

        for topic_id in range(0, self.num_topics):
            word_coef = word_topics[topic_id]

            if word_coef >= minimum_probability:
                values.append((topic_id, word_coef))

        return values

    def get_document_topics(self, bow, minimum_probability=None,
                            normalize=None):
        """Get the topic distribution for the given document.

        Parameters
        ----------
        bow : list of (int, float)
            The document in BOW format.
        minimum_probability : float
            If `normalize` is True, than only topics with larger probabilities than this are included in a result.
            If `normalize` is False, than only topics with larger factors than this are included in a result.
            If set to None, a value of 1e-8 is used to prevent 0s.
        normalize: bool, optional
            Whether to normalize the result. Allows for estimation of perplexity, coherence, e.t.c.

        Returns
        -------
        list of (int, float)
            Topic distribution for the whole document. Each element in the list is a pair of a topic's id, and
            the probability that was assigned to it.

        """
        if minimum_probability is None:
            minimum_probability = self.minimum_probability
        minimum_probability = max(minimum_probability, 1e-8)

        # if the input vector is a corpus, return a transformed corpus
        is_corpus, corpus = utils.is_corpus(bow)

        if is_corpus:
            kwargs = dict(minimum_probability=minimum_probability)
            return self._apply(corpus, **kwargs)

        v = matutils.corpus2csc([bow], self.num_tokens)
        h, _ = self._solveproj(v, self._W, v_max=np.inf)

        if normalize is None:
            normalize = self.normalize
        if normalize:
            h /= h.sum()

        return [
            (idx, proba)
            for idx, proba in enumerate(h[:, 0])
            if not minimum_probability or proba > minimum_probability
        ]

    def _setup(self, corpus):
        """Infer info from the first document and initialize matrices.

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.

        """
        self._h, self._r = None, None
        first_doc_it = itertools.tee(corpus, 1)
        first_doc = next(first_doc_it[0])
        first_doc = matutils.corpus2csc([first_doc], len(self.id2word))
        self.w_std = np.sqrt(first_doc.mean() / (self.num_tokens * self.num_topics))

        self._W = np.abs(
            self.w_std
            * halfnorm.rvs(
                size=(self.num_tokens, self.num_topics), random_state=self.random_state
            )
        )

        self.A = np.zeros((self.num_topics, self.num_topics))
        self.B = np.zeros((self.num_tokens, self.num_topics))

    def update(self, corpus, chunks_as_numpy=False):
        """Train the model with new documents.

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.
        chunks_as_numpy : bool, optional
            Deprecated.

        """

        if self._W is None:
            self._setup(corpus)

        chunk_idx = 1

        for _ in range(self.passes):
            for chunk in utils.grouper(
                corpus, self.chunksize, as_numpy=chunks_as_numpy
            ):
                self.random_state.shuffle(chunk)
                v = matutils.corpus2csc(
                    chunk,
                    num_terms=self.num_tokens,
                )
                self._h, self._r = self._solveproj(
                    v, self._W, r=self._r, h=self._h, v_max=self.v_max
                )
                h, r = self._h, self._r

                self.A *= chunk_idx - 1
                self.A += h.dot(h.T)
                self.A /= chunk_idx

                self.B *= chunk_idx - 1
                self.B += (v - r).dot(h.T)
                self.B /= chunk_idx

                self._solve_w()

                if chunk_idx % self.eval_every == 0:
                    Wt = self._W.T
                    Wtv = self._dense_dot_csc(Wt, v)
                    Wtr = self._dense_dot_csc(Wt, r)
                    WtWh = Wt.dot(self._W).dot(h)

                    logger.info(
                        "Loss (no outliers): {}\tLoss (with outliers): {}".format(
                            np.linalg.norm(Wtv - WtWh),
                            np.linalg.norm(Wtv - WtWh - Wtr),
                        )
                    )

                chunk_idx += 1

        Wt = self._W.T
        Wtv = self._dense_dot_csc(Wt, v)
        Wtr = self._dense_dot_csc(Wt, r)
        WtWh = Wt.dot(self._W).dot(h)

        logger.info(
            "Loss (no outliers): {}\tLoss (with outliers): {}".format(
                np.linalg.norm(Wtv - WtWh),
                np.linalg.norm(Wtv - WtWh - Wtr),
            )
        )

    def _solve_w(self):
        """Update W matrix."""

        def error():
            Wt = self._W.T
            return (
                0.5 * Wt.dot(self._W).dot(self.A).trace()
                - Wt.dot(self.B).trace()
            )

        eta = self._kappa / np.linalg.norm(self.A)

        for iter_number in range(self._w_max_iter):
            logger.debug("w_error: %s" % self._w_error)

            error_ = error()

            if (
                self._w_error
                and np.abs((error_ - self._w_error) / self._w_error) < self._w_stop_condition
            ):
                break

            self._w_error = error_

            self._W -= eta * (self._W.dot(self.A) - self.B)
            self._transform()

    def _apply(self, corpus, chunksize=None, **kwargs):
        """Apply the transformation to a whole corpus and get the result as another corpus.

        Parameters
        ----------
        corpus : iterable of list of (int, float), optional
            Training corpus. Contains list of counts of words for every document.
        chunksize : int, optional
            If provided, a more effective processing will performed.

        Returns
        -------
        :class:`~gensim.interfaces.TransformedCorpus`
            Transformed corpus.

        """
        return TransformedCorpus(self, corpus, chunksize, **kwargs)

    def _transform(self):
        """Apply boundaries on W."""
        np.clip(self._W, 0, self.v_max, out=self._W)
        sumsq = np.linalg.norm(self._W, axis=0)
        np.maximum(sumsq, 1, out=sumsq)
        self._W /= sumsq

    @staticmethod
    def _dense_dot_csc(dense, csc):
        if OLD_SCIPY:
            return (csc.T.dot(dense.T)).T
        else:
            return scipy.sparse.csc_matrix.dot(dense, csc)

    def _solveproj(self, v, W, h=None, r=None, v_max=None):
        """Update residuals and representation(h) matrices.

        Parameters
        ----------
        v : scipy.sparse.csc_matrix
            Subset of training corpus.
        W : ndarray
            Dictionary matrix.
        h : ndarray
            Representation matrix.
        r : scipy.sparse.csc_matrix
            Residuals matrix.
        v_max : float
            Maximum possible value in matrices.

        """
        m, n = W.shape
        if v_max is not None:
            self.v_max = v_max
        elif self.v_max is None:
            self.v_max = v.max()

        batch_size = v.shape[1]
        rshape = (m, batch_size)
        hshape = (n, batch_size)

        if h is None or h.shape != hshape:
            h = np.zeros(hshape)

        if r is None or r.shape != rshape:
            r = scipy.sparse.csc_matrix(rshape)

        Wt = W.T
        WtW = Wt.dot(W)

        h_r_error = None

        for iter_number in range(self._h_r_max_iter):
            logger.debug("h_r_error: %s" % h_r_error)

            Wt_v_minus_r = self._dense_dot_csc(Wt, v - r)

            permutation = self.random_state.permutation(self.num_topics)

            error_ = solve_h(h, Wt_v_minus_r, WtW, permutation, self._kappa)

            error_ /= m

            if h_r_error and np.abs(h_r_error - error_) < self._h_r_stop_condition:
                break

            h_r_error = error_

        return h, r
