import copy
import h5py
import sys
import typing as t
import warnings
from math import ceil
from pathlib import Path

import numpy as np
import tensorflow as tf
from ampal.amino_acids import standard_amino_acids
from numpy import genfromtxt
from tqdm import tqdm

from aposteriori.data_prep.create_frame_data_set import DatasetMetadata
from aposteriori.config import UNCOMMON_RESIDUE_DICT, MAKE_FRAME_DATASET_VER
from tensorflow.keras.metrics import top_k_categorical_accuracy


def top_3_cat_acc(y_true, y_pred):
    return top_k_categorical_accuracy(y_true, y_pred, k=3)


tf.keras.utils.get_custom_objects()["top_3_cat_acc"] = top_3_cat_acc


def extract_metadata_from_dataset(frame_dataset: Path) -> DatasetMetadata:
    """
    Retrieves the metadata of the dataset and does a sanity check of the version.
    If the dataset version is not compatible with aposteriori, the training process will stop.

    Parameters
    ----------
    frame_dataset: Path
        Path to the .h5 dataset with the following structure.
        └─[pdb_code] Contains a number of subgroups, one for each chain.
          └─[chain_id] Contains a number of subgroups, one for each residue.
            └─[residue_id] voxels_per_side^3 array of ints, representing element number.
              └─.attrs['label'] Three-letter code for the residue.
              └─.attrs['encoded_residue'] One-hot encoding of the residue.
        └─.attrs['make_frame_dataset_ver']: str - Version used to produce the dataset.
        └─.attrs['frame_dims']: t.Tuple[int, int, int, int] - Dimentsions of the frame.
        └─.attrs['atom_encoder']: t.List[str] - Lables used for the encoding (eg, ["C", "N", "O"]).
        └─.attrs['encode_cb']: bool - Whether a Cb atom was added at the avg position of (-0.741287356, -0.53937931, -1.224287356).
        └─.attrs['atom_filter_fn']: str - Function used to filter the atoms in the frame.
        └─.attrs['residue_encoder']: t.List[str] - Ordered list of residues corresponding to the encoding used.
        └─.attrs['frame_edge_length']: float - Length of the frame in Angstroms (A)
        └─.attrs['voxels_as_gaussian']: bool - Whether the voxels are encoded as a floating point of a gaussian (True) or boolean (False)


    Returns
    -------
    dataset_metadata: DatasetMetadata of the dataset with the following parameters:
        make_frame_dataset_ver: str
        frame_dims: t.Tuple[int, int, int, int]
        atom_encoder: t.List[str]
        encode_cb: bool
        atom_filter_fn: str
        residue_encoder: t.List[str]
        frame_edge_length: float
        voxels_as_gaussian: bool

    """
    with h5py.File(frame_dataset, "r") as dataset_file:
        meta_dict = dict(dataset_file.attrs.items())
        dataset_metadata = DatasetMetadata.import_metadata_dict(meta_dict)

    # Extract version metadata:
    dataset_ver_num = dataset_metadata.make_frame_dataset_ver.split(".")[0]
    aposteriori_ver_num = MAKE_FRAME_DATASET_VER.split(".")[0]
    # If the versions are compatible, return metadata else stop:
    if dataset_ver_num != aposteriori_ver_num:
        sys.exit(
            f"Dataset version is {dataset_metadata.make_frame_dataset_ver} and is incompatible "
            f"with Aposteriori version {MAKE_FRAME_DATASET_VER}."
            f"Try re-creating the dataset with the current version of Aposteriori."
        )
    return dataset_metadata


def get_pdb_keys_to_filter(
    pdb_key_path: Path, file_extension: str = ".txt"
) -> t.List[str]:
    """
    Obtains list of PDB keys from benchmark file. This is to ensure no leakage
    of training samples is seen in the benchmark.

    Parameters
    ----------
    pdb_key_path: Path
        Path to files with pdb keys.
    file_extension: str
        Extension of file. Defaults to ".txt"

    Returns
    -------
    pdb_keys_list: t.List[str]
        List of pdb keys to be removed from training set.
    """
    pdb_key_files = list(pdb_key_path.glob(f"**/*{file_extension}"))
    assert len(pdb_key_files) >= 1, "Expected at least 1 pdb key file."

    pdb_keys_list = []
    # For each file:
    for pdb_list_file in pdb_key_files:
        curr_keys_list = genfromtxt(pdb_list_file, dtype=str)
        # filter chain (we want to delete the whole structure, regardless of chain:
        for pdb in curr_keys_list:
            # Add to list:
            pdb_keys_list.append(pdb[:4])

    return pdb_keys_list


def create_flat_dataset_map(
    frame_dataset: Path,
    filter_list: t.List[str] = [],
    remove_blacklist_silently: bool = False,
) -> (t.List[t.Tuple[str, int, str, str]], t.Set[str]):
    """
    Flattens the structure of the h5 dataset for batching and balancing
    purposes.

    Parameters
    ----------
    frame_dataset: Path
        Path to the .h5 dataset with the following structure.
        └─[pdb_code] Contains a number of subgroups, one for each chain.
          └─[chain_id] Contains a number of subgroups, one for each residue.
            └─[residue_id] voxels_per_side^3 array of ints, representing element number.
              └─.attrs['label'] Three-letter code for the residue.
              └─.attrs['encoded_residue'] One-hot encoding of the residue.
        └─.attrs['make_frame_dataset_ver']: str - Version used to produce the dataset.
        └─.attrs['frame_dims']: t.Tuple[int, int, int, int] - Dimentsions of the frame.
        └─.attrs['atom_encoder']: t.List[str] - Lables used for the encoding (eg, ["C", "N", "O"]).
        └─.attrs['encode_cb']: bool - Whether a Cb atom was added at the avg position of (-0.741287356, -0.53937931, -1.224287356).
        └─.attrs['atom_filter_fn']: str - Function used to filter the atoms in the frame.
        └─.attrs['residue_encoder']: t.List[str] - Ordered list of residues corresponding to the encoding used.
        └─.attrs['frame_edge_length']: float - Length of the frame in Angstroms (A)
    filter_list: t.List[str]
        List of banned PDBs. These are automatically removed from the train/validation set.
    remove_blacklist_silently: bool
        Whether to remove the pdb codes in the blacklist with a warning (True), or raise ValueError (False and default)
    Returns
    -------
    flat_dataset_map: t.List[t.Tuple]
        List of tuples with the order
        [... (pdb_code, chain_id, residue_id,  residue_label, encoded_residue) ...]
    training_set_pdbs: set
        Set of all the pdb codes in the training/validation set.
    """
    standard_residues = list(standard_amino_acids.values())
    # Training set pdbs:
    training_set_pdbs = set()

    with h5py.File(frame_dataset, "r") as dataset_file:
        flat_dataset_map = []
        # Create flattened dataset structure:
        for pdb_code in dataset_file:
            # Check first 4 letters of PBD code in blacklist:
            if pdb_code[:4] not in filter_list:
                for chain_id in dataset_file[pdb_code].keys():
                    # Sort by residue int rather than str
                    residue_n = np.array(
                        list(dataset_file[pdb_code][chain_id].keys()), dtype=np.int
                    )
                    residue_n.sort()
                    residue_n = np.array(residue_n, dtype=str)
                    for residue_id in residue_n:
                        # Extract residue info:
                        residue_label = dataset_file[pdb_code][chain_id][
                            str(residue_id)
                        ].attrs["label"]

                        if residue_label in standard_residues:
                            pass
                        # If uncommon, attempt conversion of label
                        elif residue_label in UNCOMMON_RESIDUE_DICT.keys():
                            warnings.warn(f"{residue_label} is not a standard residue.")
                            # Convert residue to common residue
                            residue_label = UNCOMMON_RESIDUE_DICT[residue_label]
                            warnings.warn(f"Residue converted to {residue_label}.")
                        else:
                            assert (
                                residue_label in standard_residues
                            ), f"Expected natural amino acid, but got {residue_label}."

                        flat_dataset_map.append(
                            (pdb_code, chain_id, residue_id, residue_label)
                        )
                        training_set_pdbs.add(pdb_code)
            else:
                if remove_blacklist_silently:
                    warnings.warn(
                        f"PDB code {pdb_code} was found in benchmark dataset. It was automatically removed."
                    )
                else:
                    raise ValueError(
                        f"PDB code {pdb_code} was found in benchmark dataset. "
                        f"Turn on remove_blacklist_silently=True if you want to"
                        f" ignore these structures for training."
                    )

    return flat_dataset_map, training_set_pdbs


def load_batch(
    dataset_path: Path, data_point_batch: t.List[t.Tuple]
) -> (np.ndarray, np.ndarray):
    """
    Load batch from a dataset map.

    Parameters
    ----------
    dataset_path: Path
        Path to the dataset
    data_point_batch: t.List[t.Tuple]
        Flat dataset map of current batch

    Returns
    -------
    X: np.ndarray
        5D frames with (batch_size, n, n, n, n_encoding) shape
    y: np.ndarray
        Array of shape (batch_size, 20) containing labels of frames

    """
    # Calcualte catch size
    batch_size = len(data_point_batch)
    # Open hdf5:
    with h5py.File(str(dataset_path), "r") as dataset:
        dims = dataset.attrs["frame_dims"]
        voxels_as_gaussian = dataset.attrs["voxels_as_gaussian"]
        # Initialize X and y:
        if voxels_as_gaussian:
            X = np.empty((batch_size, *dims), dtype=float)
        else:
            X = np.empty((batch_size, *dims), dtype=bool)
        y = np.empty((batch_size, 20), dtype=float)
        # Extract frame from batch:
        for i, (pdb_code, chain_id, residue_id, _) in enumerate(data_point_batch):
            # Extract frame:
            residue_frame = np.asarray(dataset[pdb_code][chain_id][residue_id][()])
            X[i] = residue_frame
            # Extract residue label:
            y[i] = dataset[pdb_code][chain_id][residue_id].attrs["encoded_residue"]

    return X, y


def load_dataset_and_predict(
    models: list,
    dataset_path: Path,
    batch_size: int = 20,
    start_batch: int = 0,
    dataset_map_path: Path = "dataset",
    blacklist: Path = None,
) -> np.ndarray:
    """
    Load discretized frame dataset (should be the same format as the trained models),
    creates a dataset map and predicts the frames using each of the models.

    Everything is then saved into a csv file.

    Parameters
    ----------
    models: t.List[StrOrPath]
        List of paths to the models to be used for the ensemble
    dataset_path: Path
        Path to the dataset with frames.
    batch_size: int
        Number of frames to be looked predicted at once.
    start_batch:
        Which batch to start from. In case the code crashes you can check which
        was the last batch used and restart from there. Make sure you remove the
        other models from the paths to be used.

    Returns
    -------
    flat_dataset_map: t.List[t.Tuple]
        List of tuples with the order
        [... (pdb_code, chain_id, residue_id,  residue_label, encoded_residue) ...]

    """
    # Get list of banned pdbs from the benchmark:
    if blacklist:
        filter_pdb_list = get_pdb_keys_to_filter(blacklist)
    else:
        filter_pdb_list = []
    # If dataset map exists, load it from path:
    if Path(dataset_map_path).exists():
        flat_dataset_map = genfromtxt(dataset_map_path, delimiter=",", dtype="str")
    else:
        # Create flat_map:
        flat_dataset_map, training_set_pdbs = create_flat_dataset_map(
            dataset_path, filter_pdb_list
        )

    # Calculate number of batches
    n_batches = ceil(len(flat_dataset_map) / batch_size)
    # For each model:
    for i, m in enumerate(models):
        # Extract model names:
        if isinstance(m, Path):
            model_name = m.stem
        else:
            model_name = str(m)
        # Import Model:
        frame_model = tf.keras.models.load_model(m)
        # Load batch:
        for index in tqdm(
            range(start_batch, n_batches),
            desc=f"Processing batch of model {model_name}",
        ):
            # Initialize array for predictions:
            y_true = []
            # Initialize dictionary with {model_number : [predictions]}
            y_pred = {k: [] for k in range(len(models))}
            # Extract current batch map:
            current_batch_map = flat_dataset_map[
                index * batch_size : (index + 1) * batch_size
            ]
            X_batch, y_true_batch = load_batch(dataset_path, current_batch_map)
            # Make Predictions
            y_pred_batch = frame_model.predict(X_batch)
            # Add predictions labels to dictionary:
            y_pred[i].extend(y_pred_batch)
            # Save current labels:
            y_true.extend(y_true_batch)
            # Save to output file:
            save_outputs_to_file(y_true, y_pred, flat_dataset_map, i, model_name)
            # Reset to avoid memory errors
            del y_true
            del y_pred
        flat_dataset_map = np.array(flat_dataset_map)
        # Output datasetmap compatible with sequence recovery benchmark:
        with open(f"{model_name}.txt", "w") as f:
            pdbcode_and_chain = np.core.defchararray.add(
                flat_dataset_map[:, 0], flat_dataset_map[:, 1]
            )
            pdb_code, count = np.unique(pdbcode_and_chain, return_counts=True)
            srb_dataset_map = np.hstack((pdb_code, count))
            np.savetxt(f, srb_dataset_map, delimiter=" ", fmt="%s")
        # Load prediction matrix
        prediction_matrix = genfromtxt(
            f"{model_name}.csv", delimiter=",", dtype=np.float16
        )
        # Save as Fasta file:
        (
            pdb_to_sequence,
            pdb_to_probability,
            pdb_to_real_sequence,
            pdb_to_consensus,
            pdb_to_consensus_prob,
        ) = extract_sequence_from_pred_matrix(flat_dataset_map, prediction_matrix)
        save_dict_to_fasta(pdb_to_sequence, model_name)
        save_dict_to_fasta(pdb_to_real_sequence, "dataset")
        if pdb_to_consensus:
            save_dict_to_fasta(pdb_to_real_sequence, model_name+"_consensus")
            save_consensus_probs(pdb_to_consensus_prob, model_name)

    return flat_dataset_map


def save_consensus_probs(pdb_to_consensus_prob: dict, model_name:str):
    """
    Saves consensus sequence into PDBench-compatible format.

    Parameters
    ----------
    pdb_to_consensus_prob: dict

    model_name: dict

    """
    with open(f"{model_name}_consensus.txt", 'w') as d, open(f"{model_name}_consensus.csv", 'a') as p:
        for pdb, predictions in pdb_to_consensus_prob.items():
            d.write(f"{pdb} {len(predictions)}\n")
            np.savetxt(p, predictions, delimiter=",")


def save_dict_to_fasta(pdb_to_sequence: dict, model_name: str):
    """
    Saves a dictionary of protein sequences to a fasta file.

    Parameters
    ----------
    pdb_to_sequence: dict
        Dictionary {pdb_code: predicted_sequence}
    model_name: str
        Name of the model.
    """
    with open(f"{model_name}.fasta", "w") as f:
        f.write("ignore_uncommon False\ninclude_pdbs\n##########\n")
        for pdb, seq in pdb_to_sequence.items():
            f.write(f">{pdb}\n{seq}\n")


def extract_sequence_from_pred_matrix(
    flat_dataset_map: t.List[t.Tuple], prediction_matrix: np.ndarray
) -> (dict, dict, dict, dict, dict):
    """
    Extract sequence from prediction matrix and create pdb_to_sequence and
    pdb_to_probability dictionaries

    Parameters
    ----------
    flat_dataset_map: t.List[t.Tuple]
        List of tuples with the order
        [... (pdb_code, chain_id, residue_id,  residue_label, encoded_residue) ...]
    prediction_matrix: np.ndarray
        Prediction matrix for each of the sequence

    Returns
    -------
    pdb_to_sequence: dict
        Dictionary {pdb_code: predicted_sequence}
    pdb_to_sequence: dict
        Dictionary {pdb_code: sequence}
    pdb_to_probability: dict
        Dictionary {pdb_code: probability}
    """
    pdb_to_sequence = {}
    pdb_to_probability = {}
    pdb_to_real_sequence = {}
    pdb_to_consensus = {}
    pdb_to_consensus_prob = {}
    # Wether the dataset contains multiple states of NMR or not
    is_consensus = False

    res_dic = list(standard_amino_acids.keys())
    res_to_r_dic = dict(zip(standard_amino_acids.values(), standard_amino_acids.keys()))
    max_idx = np.argmax(prediction_matrix, axis=1)

    for i in range(len(flat_dataset_map)):
        pdb, chain, _, res = flat_dataset_map[i]
        if "_" in pdb:
            pdbchain = pdb
            is_consensus = True
        else:
            pdbchain = pdb + chain

        if pdbchain not in pdb_to_sequence:
            pdb_to_sequence[pdbchain] = ""
            pdb_to_real_sequence[pdbchain] = ""
            pdb_to_probability[pdbchain] = []

        pred = list(prediction_matrix[i])
        curr_res = res_dic[max_idx[i]]

        pdb_to_probability[pdbchain].append(pred)
        pdb_to_sequence[pdbchain] += curr_res
        pdb_to_real_sequence[pdbchain] += res_to_r_dic[res]

    if is_consensus:
        last_pdb = ""
        # Sum up probabilities:
        for pdb in pdb_to_sequence.keys():
            curr_pdb = pdb[:5]
            if last_pdb != curr_pdb:
                pdb_to_consensus[curr_pdb] = ""
                pdb_to_consensus_prob[curr_pdb] = np.array(pdb_to_probability[pdb])
                last_pdb = curr_pdb
            else:
                pdb_to_consensus_prob[curr_pdb] = (
                    pdb_to_consensus_prob[curr_pdb] + np.array(pdb_to_probability[pdb])
                ) / 2
        # Extract sequences from consensus probabilities:
        for pdb in pdb_to_consensus.keys():
            curr_prob = pdb_to_consensus_prob[pdb]
            max_idx = np.argmax(curr_prob, axis=1)
            for m in max_idx:
                curr_res = res_dic[max_idx[m]]
                pdb_to_consensus[pdb] += curr_res

        return (
            pdb_to_sequence,
            pdb_to_probability,
            pdb_to_real_sequence,
            pdb_to_consensus,
            pdb_to_consensus_prob,
        )
    else:
        return pdb_to_sequence, pdb_to_probability, pdb_to_real_sequence, None, None


def save_outputs_to_file(
    y_true: np.ndarray,
    y_pred: np.ndarray,
    flat_dataset_map: t.List[t.Tuple],
    model: int,
    model_name: str,
):
    """
    Saves predictions for a specific model to file.

    Parameters
    ----------
    y_true: np.ndarray
        Numpy array of labels (int) 0 or 1.
    y_pred: np.ndarray
        Numpy array of predictions (float) range 0 - 1
    flat_dataset_map: t.List[t.Tuple]
        List of tuples with the order
        [... (pdb_code, chain_id, residue_id,  residue_label, encoded_residue) ...]
    model: int
        Number of the model being used.
    model_name: int
        Name of the model being used.
    """
    # Save dataset map only at the beginning:
    if model == 0:
        with open("encoded_labels.csv", "a") as f:
            y_true = np.asarray(y_true)
            np.savetxt(f, y_true, delimiter=",", fmt="%i")
    flat_dataset_map = np.asarray(flat_dataset_map)
    # Save dataset map only at the beginning:
    if Path("datasetmap.txt").exists() == False:
        with open("datasetmap.txt", "a") as f:
            # Output Dataset Map to txt:
            np.savetxt(f, flat_dataset_map, delimiter=",", fmt="%s")

    predictions = np.array(y_pred[model], dtype=np.float16)
    # Output model predictions:
    with open(f"{model_name}.csv", "a") as f:
        np.savetxt(f, predictions, delimiter=",")
