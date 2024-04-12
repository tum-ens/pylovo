from math import pi

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from sklearn import preprocessing
from sklearn.cluster import KMeans
from sklearn.decomposition import FactorAnalysis, PCA
from sklearn.metrics import calinski_harabasz_score
from sklearn.metrics import davies_bouldin_score
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler
from sklearn_extra.cluster import KMedoids

from classification.clustering.config_clustering import *
from classification.config_classification import REGIO7_REGIO5_GEM_DICT
from plotting.config_plots import *


def plot_correlation_matrix(corr):
    """plot correlation matrix"""
    fig, ax = plt.subplots()
    sns.heatmap(corr, annot=True, fmt='.2f',
                cmap=plt.get_cmap('coolwarm'), cbar=True, ax=ax, vmin=-1, vmax=1)
    ax.set_yticklabels(ax.get_yticklabels(), rotation='horizontal')
    # plt.colorbar(fig).ax.set_ylabel("$r$", rotation=0)
    fig.set_size_inches(9, 9)
    plt.subplots_adjust(bottom=0.3, left=0.3)
    plt.savefig('corrmatrix.png', dpi=600)
    plt.show()


def plot_samples_per_regiostarclass(df_samples: pd.DataFrame) -> None:
    """plot bar chart with number of samples per regiostar 7 class
    """
    plt.figure(figsize=(9, 6))
    ax = sns.countplot(data=df_samples, x="regio7", palette=TUMPalette1)
    ax.bar_label(ax.containers[0])
    plt.xlabel('Regiostar 7 Klasse', fontsize=18);
    plt.ylabel('Anzahl der Stichproben', fontsize=18);
    plt.tick_params(axis='both', which='major', labelsize=16)
    # plt.savefig('anz_samples.png', dpi=600)


def plot_radar_graph(representative_networks: pd.DataFrame, list_of_parameters: list) -> None:
    """plots the representative networks of a clustering algorithm as radar graphs

    :param representative_networks: table of parameters of representative networks
    :type representative_networks: pd.DataFrame

    :param list_of_parmeters: parameters that are plotted in radar graph
    :type list_of_parmeters: list of strings
    """
    # reduce the parameters of representative networks to those that will be plotted
    representative_networks.reset_index(inplace=True)
    list_of_parameters.append('clusters')
    representative_networks = representative_networks[list_of_parameters]

    # compute max values of each column
    max_values = representative_networks.max()

    # normalize values of each column with max values
    representative_networks_normalized = representative_networks
    for column in representative_networks:
        representative_networks_normalized[column] = representative_networks[column] / max_values[column]

    # move clusters column to first position
    first_column = representative_networks_normalized.pop('clusters')
    representative_networks_normalized.insert(0, 'clusters', first_column)

    # ------- PART 1: Define a function that do a plot for one line of the dataset!
    def make_spider(row, title, color):
        # number of variable
        categories = list(representative_networks_normalized)[1:]
        N = len(categories)

        # What will be the angle of each axis in the plot? (we divide the plot / number of variable)
        angles = [n / float(N) * 2 * pi for n in range(N)]
        angles += angles[:1]

        # Initialise the spider plot
        ax = plt.subplot(2, 3, row + 1, polar=True, )

        # If you want the first axis to be on top:
        ax.set_theta_offset(pi / 2)
        ax.set_theta_direction(-1)

        # Draw one axe per variable + add labels labels yet
        plt.xticks(angles[:-1], categories, color='grey', size=8)
        plt.gcf().canvas.draw()
        angles_text = [0, 90, 0, 90, 0]
        labels = []
        for label, angle in zip(ax.get_xticklabels(), angles_text):
            x, y = label.get_position()
            lab = ax.text(x, y, label.get_text(), transform=label.get_transform(),
                          ha=label.get_ha(), va=label.get_va(), color='grey', size=8)
            lab.set_rotation(angle)
            labels.append(lab)
        ax.set_xticklabels([])

        # Draw ylabels
        ax.set_rlabel_position(0)
        plt.yticks([0.33, 0.66, 1.0], ["33%", "66%", "100%"], color="grey", size=7)
        plt.ylim(0, 1.1)

        # Ind1
        values = representative_networks_normalized.loc[row].drop('clusters').values.flatten().tolist()
        values += values[:1]
        ax.plot(angles, values, color=color, linewidth=2, linestyle='solid')
        ax.fill(angles, values, color=color, alpha=0.4)

        # Add a title
        plt.title(title, size=11, color=color, y=1.1)

    # ------- PART 2: Apply the function to all individuals
    # initialize the figure
    my_dpi = 96
    plt.figure(figsize=(1000 / my_dpi, 1000 / my_dpi), dpi=my_dpi)

    # Loop to plot
    for row in range(0, len(representative_networks.index)):
        make_spider(row=row, title=row, color=TUMPalette[row])

    # plt.savefig('radar_plot.pdf', dpi=600)


def plot_samples_on_map(df_samples: pd.DataFrame) -> None:
    """plot the PLZ in the sample set on a plotly map
    """
    df_samples['regio7_str'] = df_samples['regio7'].astype("str")
    fig = px.scatter_mapbox(df_samples, lat="lat", lon="lon", color="regio7_str", size="regio7", size_max=10,
                            zoom=7)  # marker = {"size": 10})
    fig.update_layout(width=1000, height=900, margin={"r": 5, "t": 0, "l": 5, "b": 0}, )
    fig.update_layout(mapbox_style="light")
    fig.show()


def plot_factor_analysis(df_grid_parameters: pd.DataFrame, n_comps: int) -> None:
    """plot factor analysis for dataset

    :param df_grid_parameters: set of parameters for grids
    :type df_grid_parameters: pd.DataFrame
    :param n_comps: number of components for factor analysis
    :type n_comps: int
    """
    # scale data and save parameter names
    data = df_grid_parameters
    X = StandardScaler().fit_transform(data)
    feature_names = data.columns

    # define methods
    methods = [
        ("PCA", PCA()),
        ("Unrotated FA", FactorAnalysis()),
        ("Varimax FA", FactorAnalysis(rotation="varimax")),
    ]
    fig, axes = plt.subplots(ncols=len(methods), figsize=(10, 8), sharey=True)

    # plot methods
    for ax, (method, fa) in zip(axes, methods):
        fa.set_params(n_components=n_comps)
        fa.fit(X)

        components = fa.components_.T
        # print("\n\n %s :\n" % method)
        # print(components)

        vmax = 1  # np.abs(components).max()
        ax.imshow(components, cmap="RdBu_r", vmax=vmax, vmin=-vmax)
        ax.set_yticks(np.arange(len(feature_names)))
        ax.set_yticklabels(feature_names)
        ax.set_title(str(method))
        ax.set_xticks(range(0, n_comps))
        # ax.set_xticklabels(["Comp. 1", "Comp. 2"])
    fig.suptitle("Factors")
    plt.tight_layout()
    # plt.savefig('factoranalysis.png', dpi=600)
    plt.show()


def get_parameters_for_clustering(df_grid_parameters: pd.DataFrame, n_comps: int) -> None:
    """calculate the mathematically ideal set of parameters
    with the varimax rotated factor analysis

    :param df_grid_parameters: set of parameters for grids
    :type df_grid_parameters: pd.DataFrame
    :param n_comps: number of components for factor analysis
    :type n_comps: int
    """

    data = df_grid_parameters
    X = StandardScaler().fit_transform(data)
    feature_names = data.columns
    fa = FactorAnalysis(rotation="varimax")
    fa.set_params(n_components=n_comps)
    fa.fit(X)

    df_components_fa = pd.DataFrame(fa.components_.T, index=feature_names)
    for column in df_components_fa:
        print(df_components_fa[column].abs().idxmax())


def plot_eigendecomposition(df_grid_parameters: pd.DataFrame) -> None:
    """plot the explained variance of the principal components

    :param df_grid_parameters: set of parameters for grids
    :type df_grid_parameters: pd.DataFrame
    """
    X_train = df_grid_parameters
    #
    # Scale the dataset; This is very important before you apply PCA
    #
    sc = StandardScaler()
    sc.fit(X_train)
    X_train_std = sc.transform(X_train)
    # X_test_std = sc.transform(X_test)
    #
    # Instantiate PCA
    #
    pca = PCA()
    #
    # Determine transformed features
    #
    X_train_pca = pca.fit_transform(X_train_std)
    #
    # Determine explained variance using explained_variance_ration_ attribute
    #
    exp_var_pca = pca.explained_variance_ratio_
    #
    # Cumulative sum of eigenvalues; This will be used to create step plot
    # for visualizing the variance explained by each principal component.
    #
    cum_sum_eigenvalues = np.cumsum(exp_var_pca)
    #
    # Create the visualization plot
    #
    plt.bar(range(0, len(exp_var_pca)), exp_var_pca, alpha=0.5, align='center',
            label='Erklärte Varianz der einzelnen Faktoren')  # 'Individual explained variance')
    plt.step(range(0, len(cum_sum_eigenvalues)), cum_sum_eigenvalues, where='mid',
             label='Kumulativ erklärte Varianz')  # 'Cumulative explained variance')
    plt.ylabel('Anteil der erklärten Varianz')  # 'Explained variance ratio')
    plt.xlabel('Index des Faktors')  # Principal component index')
    plt.legend(loc='best')
    plt.tight_layout()
    plt.subplots_adjust(bottom=0.3, left=0.3)
    # plt.savefig('eigendecomposition.png', dpi=600)
    plt.show()


def plot_ch_index_for_clustering_algos(df_grid_parameters: pd.DataFrame,
                                       no_of_clusters_allowed: range = range(3, 8)) -> None:
    """plot the calinski harabasz index for the indicated range of clusters and the clusterin algorithms kmeans,
    kmedoids, gmm

    :param df_grid_parameters: set of parameters for grids
    :type df_grid_parameters: pd.DataFrame

    :param no_of_clusters_allowed: the range the number of clusters allowed to have, , defaults range(3, 8)
    :type no_of_clusters_allowed: range
    """
    # define dataset for ch index analysis
    X = df_grid_parameters[LIST_OF_CLUSTERING_PARAMETERS]
    X = preprocessing.scale(X)

    # initialise table for comparison
    df_ch_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'ch_index'])

    # kmeans: cluster, calculate ch index for all numbers of clusters
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        # K-Means
        kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
        # we store the cluster labels
        labels = kmeans.labels_
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    data = {'no_clusters': no_of_clusters_allowed,
            'ch_index_kmeans': ch_list}
    # save optimal number of clusters
    df_ch_index = pd.DataFrame(data)
    idxmax = df_ch_index['ch_index_kmeans'].idxmax()
    opt_no_clusters_kmeans = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt_kmeans = df_ch_index['ch_index_kmeans'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['kmeans', opt_no_clusters_kmeans, ch_index_opt_kmeans]

    # kmedoids
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        # K-Medoids
        kmedoids = KMedoids(n_clusters=n_clusters).fit(X)
        # we store the cluster labels
        labels = kmedoids.predict(X)
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    df_ch_index['ch_index_kmedoids'] = ch_list
    idxmax = df_ch_index['ch_index_kmedoids'].idxmax()
    opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt = df_ch_index['ch_index_kmedoids'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['KMedoids', opt_no_clusters, ch_index_opt]

    # gmm full
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='full', random_state=1).fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    df_ch_index['ch_index_gmm_full'] = ch_list
    sns.lineplot(data=df_ch_index, x='no_clusters', y='ch_index_gmm_full')
    idxmax = df_ch_index['ch_index_gmm_full'].idxmax()
    opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt = df_ch_index['ch_index_gmm_full'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['GMM full', opt_no_clusters, ch_index_opt]

    # gmm diagonal
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='diag', random_state=1).fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    df_ch_index['ch_index_gmm_diag'] = ch_list
    idxmax = df_ch_index['ch_index_gmm_diag'].idxmax()
    opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt = df_ch_index['ch_index_gmm_diag'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['GMM diagonal', opt_no_clusters, ch_index_opt]

    # gmm tied
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='tied', random_state=1).fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    df_ch_index['ch_index_gmm_tied'] = ch_list
    sns.lineplot(data=df_ch_index, x='no_clusters', y='ch_index_gmm_tied')
    idxmax = df_ch_index['ch_index_gmm_tied'].idxmax()
    opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt = df_ch_index['ch_index_gmm_tied'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['GMM tied', opt_no_clusters, ch_index_opt]

    # gmm sperical
    ch_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='spherical', random_state=1).fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # ch index
        ch_index = calinski_harabasz_score(X, labels)
        ch_list.append(ch_index)
    df_ch_index['ch_index_gmm_sph'] = ch_list
    idxmax = df_ch_index['ch_index_gmm_sph'].idxmax()
    opt_no_clusters = df_ch_index.at[idxmax, 'no_clusters']
    ch_index_opt = df_ch_index['ch_index_gmm_sph'].max()
    df_ch_comparison.loc[len(df_ch_comparison)] = ['GMM spherical', opt_no_clusters, ch_index_opt]

    # plot
    ax = sns.lineplot(data=pd.melt(df_ch_index, ['no_clusters']), y='value'
                      , x='no_clusters', hue='variable', palette=TUMPalette)
    handles, labels = ax.get_legend_handles_labels()
    labels = ['kmeans', 'kmedoids', 'GMM full', 'GMM diagonal', 'GMM tied', 'GMM spherical']
    ax.legend(handles, labels, title='Clustering Algorithmus')
    ax.set(xlabel='Anzahl Cluster', ylabel='CH Index')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))
    # plt.savefig('ch_index.png', dpi=600, bbox_inches='tight')

    # print optimal number of clusters
    print(df_ch_comparison)


def plot_db_index_for_clustering_algos(df_grid_parameters: pd.DataFrame,
                                       no_of_clusters_allowed: range = range(3, 8)) -> None:
    """plot the davies bouldin index for the indicated range of clusters and the clusterin algorithms kmeans,
    kmedoids, gmm

    :param df_grid_parameters: set of parameters for grids
    :type df_grid_parameters: pd.DataFrame

    :param no_of_clusters_allowed: the range the number of clusters allowed to have, defaults range(3, 8)
    :type no_of_clusters_allowed: range
    """
    # define dataset for ch index analysis
    X = df_grid_parameters[LIST_OF_CLUSTERING_PARAMETERS]
    X = preprocessing.scale(X)

    # initialise table for comparison
    df_db_comparison = pd.DataFrame(columns=['algorithm', 'no_clusters', 'ch_index'])

    # kmeans
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        # K-Means
        kmeans = KMeans(n_clusters=n_clusters, random_state=0).fit(X)
        # we store the cluster labels
        labels = kmeans.labels_
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    data = {'no_clusters': no_of_clusters_allowed,
            'db_index_kmeans': db_list}
    df_db_index = pd.DataFrame(data)
    idxmin = df_db_index['db_index_kmeans'].idxmin()
    opt_no_clusters_kmeans = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt_kmeans = df_db_index['db_index_kmeans'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['kmeans', opt_no_clusters_kmeans, db_index_opt_kmeans]

    # kmedoids
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        # K-Medoids
        kmedoids = KMedoids(n_clusters=n_clusters).fit(X)
        # we store the cluster labels
        labels = kmedoids.predict(X)
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    df_db_index['db_index_kmedoids'] = db_list
    idxmin = df_db_index['db_index_kmedoids'].idxmin()
    opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt = df_db_index['db_index_kmedoids'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['KMedoids', opt_no_clusters, db_index_opt]

    # gmm full
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='full').fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    df_db_index['db_index_gmm_full'] = db_list
    idxmin = df_db_index['db_index_gmm_full'].idxmin()
    opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt = df_db_index['db_index_gmm_full'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['GMM full', opt_no_clusters, db_index_opt]

    # gmm diagonal
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='diag').fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    df_db_index['db_index_gmm_diag'] = db_list
    idxmin = df_db_index['db_index_gmm_diag'].idxmin()
    opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt = df_db_index['db_index_gmm_diag'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['GMM diagonal', opt_no_clusters, db_index_opt]

    # gmm tied
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='tied').fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    df_db_index['db_index_gmm_tied'] = db_list
    idxmin = df_db_index['db_index_gmm_tied'].idxmin()
    opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt = df_db_index['db_index_gmm_tied'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['GMM tied', opt_no_clusters, db_index_opt]

    # gmm spherical
    db_list = []
    for n_clusters in no_of_clusters_allowed:
        gm = GaussianMixture(n_components=n_clusters, covariance_type='spherical').fit(X)
        # we store the cluster labels
        labels = gm.predict(X)
        # db index
        db_index = davies_bouldin_score(X, labels)
        db_list.append(db_index)
    df_db_index['db_index_gmm_sph'] = db_list
    idxmin = df_db_index['db_index_gmm_sph'].idxmin()
    opt_no_clusters = df_db_index.at[idxmin, 'no_clusters']
    db_index_opt = df_db_index['db_index_gmm_sph'].min()
    df_db_comparison.loc[len(df_db_comparison)] = ['GMM spherical', opt_no_clusters, db_index_opt]

    # line plot
    ax = sns.lineplot(data=pd.melt(df_db_index, ['no_clusters']), y='value'
                      , x='no_clusters', hue='variable', palette=TUMPalette)
    handles, labels = ax.get_legend_handles_labels()
    labels = ['kmeans', 'kmedoids', 'GMM full', 'GMM diagonal', 'GMM tied', 'GMM spherical']
    ax.legend(handles, labels, title='Clustering Algorithmus')
    ax.set(xlabel='Anzahl Cluster', ylabel='DB Index')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))
    plt.savefig('db_index_results.png', dpi=600, bbox_inches='tight')

    # print optimal number of clusters
    print(df_db_comparison)


def plot_percentage_of_clusters(df_grid_parameters: pd.DataFrame) -> None:
    """plot the distribution of clusters as a bar chart

    :param df_grid_parameters: set of parameters for grids that are clustered and thus have a column named 'clusters'
    :type df_grid_parameters: pd.DataFrame
    """
    len_grids = len(df_grid_parameters)
    clusters_perc = df_grid_parameters['clusters'].value_counts() / len_grids
    clusters_perc = pd.DataFrame(clusters_perc)
    clusters_perc['count'] = clusters_perc['count'] * 100
    # clusters_perc['clusters'] = clusters_perc['clusters'] * 100
    clusters_perc = clusters_perc.sort_index()
    clusters_perc = clusters_perc.reset_index()
    sns.barplot(data=clusters_perc, y='count', x='clusters')
    # sns.barplot(data=clusters_perc, x='index', y='clusters')
    plt.ylabel('Anteil in %')
    plt.xlabel('Index des Clusters')
    plt.savefig('distribution_clusters.pdf', dpi=600)


def plot_stacked_distribution_of_clusters_per_regio_5(df_grid_parameters: pd.DataFrame) -> None:
    """plot stacked bar chart of distribution of clusters for each regio 5 class

    :param df_grid_parameters: set of parameters for grids that are clustered and thus have a column named 'clusters'
    :type df_grid_parameters: pd.DataFrame
    """
    df_grid_parameters['regio7'] = df_grid_parameters['regio7'].map(REGIO7_REGIO5_GEM_DICT)
    df_matrix = df_grid_parameters.pivot_table(index='regio7', columns='clusters', aggfunc='size')
    df_matrix = df_matrix.div(df_matrix.sum(axis=1), axis=0)
    df_matrix = df_matrix.reset_index()
    ax = df_matrix.plot(x='regio7', kind='bar', stacked=True,
                        title='Verteilung der Cluster über Regio5 Gem')
    plt.xlabel('Regionalstatistischer Gemeindetyp 5')
    labels = [item.get_text() for item in ax.get_xticklabels()]
    labels = ['51 Metropole', '52 Großstadt', '53 Mittelstadt', '54 städt. Raum', '55 dörfl. Raum']
    ax.set_xticklabels(labels)
    plt.ylabel('Anteil je Klasse (normiert)')
    sns.move_legend(ax, "upper left", bbox_to_anchor=(1, 1))
    plt.savefig('distribution_regio5_clusters.pdf', dpi=600, bbox_inches='tight')


def plot_bar_distribution_of_clusters_per_regio_5(df_grid_parameters: pd.DataFrame) -> None:
    """plot bar chart of distribution of clusters for each regio 5 class

    :param df_grid_parameters: set of parameters for grids that are clustered and thus have a column named 'clusters'
    :type df_grid_parameters: pd.DataFrame
    """
    # df_grid_parameters['regio7'] = df_grid_parameters['regio7'].map(REGIO7_REGIO5_GEM_DICT)
    # print(df_grid_parameters['regio7'].head())
    x, y = 'regio7', 'clusters'

    (df_grid_parameters
     .groupby(x)[y]
     .value_counts(normalize=True)
     .mul(100)
     .rename('percent')
     .reset_index()
     .pipe((sns.catplot, 'data'), x=x, y='percent', hue=y, kind='bar'))


def get_min_max_data_for_clusters(n_clusters: int, df_networks: pd.DataFrame) -> pd.DataFrame:
    """get min and max values of each cluster and parameter"""
    df_min_max = pd.DataFrame(
        columns=['attribute1_min', 'attribute1_max', 'attribute2_min', 'attribute2_max', 'attribute3_min',
                 'attribute3_max'])
    for n in range(0, n_clusters):
        networks_cluster_n = df_networks.groupby('clusters').get_group(n)
        df_min_max.at[n, 'attribute1_min'] = networks_cluster_n['no_house_connections'].min()
        df_min_max.at[n, 'attribute1_max'] = networks_cluster_n['no_house_connections'].max()
        df_min_max.at[n, 'attribute2_min'] = networks_cluster_n['cable_len_per_house'].min()
        df_min_max.at[n, 'attribute2_max'] = networks_cluster_n['cable_len_per_house'].max()
        df_min_max.at[n, 'attribute3_min'] = networks_cluster_n['transformer_mva'].min()
        df_min_max.at[n, 'attribute3_max'] = networks_cluster_n['transformer_mva'].max()
        print('cluster ', n, ' -----------------------------------------------------------')
        print('house connection: min,max')
        print(df_min_max.at[n, 'attribute1_min'], df_min_max.at[n, 'attribute1_max'])
        print('cable length per house: min, max')
        print(df_min_max.at[n, 'attribute2_min'], df_min_max.at[n, 'attribute2_max'])
        print('trafo: min, max')
        print(df_min_max.at[n, 'attribute3_min'], df_min_max.at[n, 'attribute3_max'])

    return df_min_max


def plot_clusters_3D(df_min_max: pd.DataFrame, df_centroids: pd.DataFrame) -> None:
    """each cluster is plotted as 3D box"""
    fig = plt.figure(figsize=(10, 10))
    ax = fig.add_subplot(111, projection='3d')
    r = [-1, 1]
    X, Y = np.meshgrid(r, r)
    for index, row in df_min_max.iterrows():
        Z = np.array([[row['attribute1_min'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_min']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_min']],
                      [row['attribute1_min'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_min'], row['attribute3_max']],
                      [row['attribute1_max'], row['attribute2_max'], row['attribute3_max']],
                      [row['attribute1_min'], row['attribute2_max'], row['attribute3_max']]])
        ax.scatter3D(Z[:, 0], Z[:, 1], Z[:, 2])
        verts = [[Z[0], Z[1], Z[2], Z[3]],
                 [Z[4], Z[5], Z[6], Z[7]],
                 [Z[0], Z[1], Z[5], Z[4]],
                 [Z[2], Z[3], Z[7], Z[6]],
                 [Z[1], Z[2], Z[6], Z[5]],
                 [Z[4], Z[7], Z[3], Z[0]]]
        ax.add_collection3d(Poly3DCollection(verts, facecolors=row['color'], linewidths=1, edgecolors='r', alpha=.20))

    ax.scatter(df_centroids[0], df_centroids[1], df_centroids[2], 'black', marker='*')
    ax.set_xlabel('No of house connections')
    ax.set_ylabel('Cable length per house (km)')
    ax.set_zlabel('Trafo Size (MVA)')
    ax.zaxis.labelpad = -0.7
    plt.show()
